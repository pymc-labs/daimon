"""Test-only in-process provider; no organization schema/policy in OS runtime."""

from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path

from daimon.core.context_provider import (
    ContextRequest,
    ContextResponse,
    EmittedCard,
    ProviderConfig,
    estimate_tokens,
)
from pydantic import BaseModel, Field


class Card(BaseModel):
    kind: str
    key: str
    source: str
    as_of: str
    text: str

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.key}"

    def render(self) -> str:
        attrs = " ".join(
            f'{k}="{html.escape(v, quote=True)}"'
            for k, v in (
                ("kind", self.kind),
                ("key", self.key),
                ("source", self.source),
                ("as_of", self.as_of),
            )
        )
        return f"<card {attrs}>{html.escape(self.text)}</card>"


class Snapshot(BaseModel):
    server_id: str
    internal_channels: tuple[str, ...]
    core_members: tuple[str, ...]
    system_fragment: str
    cards: list[Card]
    channel_claims: dict[str, list[str]] = Field(default_factory=dict)
    aliases: dict[str, list[str]] = Field(default_factory=dict)


class FixtureProvider:
    def __init__(self, path: Path, *, tenant_id: str) -> None:
        self.snapshot = Snapshot.model_validate_json(path.read_text())
        self.tenant_id = tenant_id
        self.requests: list[ContextRequest] = []

    async def __call__(self, config: ProviderConfig, request: ContextRequest) -> ContextResponse:
        self.requests.append(request)
        snap = self.snapshot
        channel = request.parent_channel_id or request.channel_id
        claims = snap.channel_claims.get(channel or "", [])
        # These lists stand in for authenticated live placement verification.
        # A body internal flag is deliberately not part of the wire contract.
        if (
            str(request.tenant_id) != self.tenant_id
            or request.server_id != snap.server_id
            or request.requester_platform_user_id not in snap.core_members
            or request.place_type == "unknown"
            or (request.place_type != "dm" and channel not in snap.internal_channels)
            or (
                request.place_type == "thread"
                and (request.parent_channel_id is None or request.channel_id != request.thread_id)
            )
        ):
            return ContextResponse(status="refused")
        index = {card.id: card for card in snap.cards}
        candidates: list[Card] = []
        seen: set[str] = set()

        def add(kind: str, key: str) -> bool:
            card_id = f"{kind}:{key}"
            if card_id in seen or card_id not in index:
                return False
            seen.add(card_id)
            candidates.append(index[card_id])
            return True

        add("person", request.requester_platform_user_id)
        if request.place_type != "dm" and len(claims) == 1:
            add("channel", channel or "")
            add("client", claims[0])
        count = 0
        for person in request.mention_ids:
            count += add("person", person)
            if count == 3:
                break
        aliases: dict[str, set[str]] = {}
        for raw, clients in snap.aliases.items():
            alias = raw.strip().casefold()
            if len(alias) >= 4:
                aliases.setdefault(alias, set()).update(clients)
        matches: list[tuple[int, str]] = []
        for alias, clients in sorted(aliases.items()):
            if len(clients) != 1:
                continue
            match = re.search(
                r"(?<!\w)" + re.escape(alias) + r"(?!\w)", request.message_text, re.IGNORECASE
            )
            if match:
                matches.append((match.start(), next(iter(clients))))
        count = 0
        for _, client in sorted(matches, key=lambda pair: pair[0]):
            count += add("client", client)
            if count == 2:
                break
        opening, closing = '<context_cards as_of="2026-10-23">\n', "\n</context_cards>\n\n"
        budget = estimate_tokens(opening + closing)
        rendered: list[str] = []
        emitted: list[EmittedCard] = []
        for card in candidates:
            text = card.render()
            digest = hashlib.sha256(text.encode()).hexdigest()
            if request.sent_hashes.get(card.id) == digest:
                continue
            cost = estimate_tokens(text) + 1
            if budget + cost > min(request.max_tokens, 2500):
                continue
            budget += cost
            rendered.append(text)
            emitted.append(
                EmittedCard(id=card.id, hash=digest, source=card.source, as_of=card.as_of)
            )
        turn = opening + "\n".join(rendered) + closing if rendered else ""
        system = "" if request.system_fragment_received else snap.system_fragment
        return ContextResponse(
            status="ok",
            system_fragment=system,
            turn_fragment=turn,
            cards=tuple(emitted),
            estimated_tokens=estimate_tokens(turn),
            provenance={"repo": "fixture", "branch": "local", "commit_sha": "fixture-v1"},
            cache_hint={"poll_seconds": 300, "last_good": True},
        )
