"""Deterministic assertions; unavailable evidence never proves absence."""

from __future__ import annotations

import re

from qa.live.errors import exception_evidence
from qa.live.schema import Assertion
from qa.live.types import (
    Backend,
    Check,
    Judge,
    Pending,
    Turn,
    obj,
    objects,
    text_components,
    text_of,
)


def evaluate(assertion: Assertion, turns: list[Turn], backend: Backend, judge: Judge) -> Check:
    kind = assertion.kind
    turn = next((t for t in turns if t.number == (assertion.turn or assertion.since_turn)), None)
    evidence: list[str] = []
    try:
        if assertion.pending_extension:
            raise Pending(assertion.pending_extension)
        if kind == "interrupt_within_s":
            raise Pending("headless interrupt hook is not implemented")
        if kind == "http_check":
            from qa.live.http_probe import check_http

            passed, reason = check_http(assertion)
        elif kind == "db_check":
            actual = backend.db_check(assertion.sql or "", turn or (turns[-1] if turns else None))
            passed = actual == assertion.expect
            reason = f"read-only query result: {actual!r}"
        else:
            if turn is None:
                raise Pending("turn was not executed")
            evidence = [
                f"https://discord.com/channels/{turn.guild_id}/"
                f"{turn.thread_id or turn.channel_id}/{m['id']}"
                for m in turn.messages
                if m.get("id")
            ]
            if kind in {"reply_within_s", "no_silent_drop", "done_within_s"}:
                duration = turn.done_s if kind == "done_within_s" else turn.first_visible_s
                passed = duration is not None and duration <= (assertion.maximum or 0)
                reason = f"observed {duration}s; maximum {assertion.maximum}s"
            elif kind in {"text_present", "text_absent"}:
                if not turn.messages:
                    raise Pending("no messages: text absence is unproven")
                # Match each content/embed component independently. Anchored
                # answers must not be joined to another message or its footer.
                pattern = assertion.pattern or ""
                components = [component for m in turn.messages for component in text_components(m)]
                match = any(re.search(pattern, component, re.MULTILINE) for component in components)
                passed = match if kind == "text_present" else not match
                reason = f"regex {assertion.pattern!r}; matched={match}"
            elif kind in {"channel_text_present", "channel_text_absent"}:
                rows = backend.channel_messages(turn)
                turn.channel_history = rows
                matched = any(
                    re.search(assertion.pattern or "", component, re.MULTILINE)
                    for row in rows
                    for component in text_components(row)
                )
                passed = matched if kind == "channel_text_present" else not matched
                reason = (
                    f"parent channel regex {assertion.pattern!r}; "
                    f"matched={matched}; messages={len(rows)}"
                )
            elif kind in {"message_count", "fences_balanced", "footer_on_last_message"}:
                if not turn.ended_at:
                    raise Pending("turn message observation was not completed")
                messages = [m for m in turn.messages if m.get("type") != 21]
                if any(not m.get("id") for m in messages):
                    raise Pending("message identity is unavailable")
                messages = list({str(m["id"]): m for m in messages}.values())
                if kind == "message_count":
                    upper_bound_exceeded = (
                        assertion.maximum is not None and len(messages) > assertion.maximum
                    )
                    if (
                        not turn.settled
                        and not upper_bound_exceeded
                        and (len(messages) < assertion.minimum or assertion.maximum is not None)
                    ):
                        raise Pending("turn did not settle; message count bound is unproven")
                    passed = len(messages) >= assertion.minimum and (
                        assertion.maximum is None or len(messages) <= assertion.maximum
                    )
                    reason = f"observed {len(messages)} unique messages"
                elif kind == "fences_balanced":
                    if not turn.settled:
                        raise Pending("turn did not settle; final code fences are unproven")
                    passed = bool(messages) and all(
                        component.count("```") % 2 == 0
                        for m in messages
                        for component in text_components(m)
                    )
                    reason = "code fences balanced in each message component"
                else:
                    if not turn.settled:
                        raise Pending("turn did not settle; final footer placement is unproven")
                    if len(messages) > 1:
                        if any(not str(m["id"]).isdigit() for m in messages):
                            raise Pending("Discord message order is unavailable")
                        messages.sort(key=lambda m: int(str(m["id"])))
                    footer_ids = [
                        str(m["id"])
                        for m in messages
                        if any(
                            re.search(
                                r"<?\$[0-9]+(?:\.[0-9]+)?\s+used\b",
                                str(obj(e.get("footer")).get("text", "")),
                            )
                            for e in objects(m.get("embeds"))
                        )
                    ]
                    last = messages[-1] if messages else {}
                    file_only = (
                        bool(last.get("attachments")) and not str(last.get("content", "")).strip()
                    )
                    passed = (
                        bool(messages) and footer_ids == [str(last.get("id"))] and not file_only
                    )
                    reason = (
                        f"cost footer ids={footer_ids}; last={last.get('id')}; "
                        f"file_only={file_only}"
                    )
            elif kind == "thread_name":
                name = backend.thread_name(turn)
                passed = (
                    (assertion.pattern is None or bool(re.search(assertion.pattern, name)))
                    and (
                        assertion.pattern_absent is None
                        or not re.search(assertion.pattern_absent, name)
                    )
                    and (assertion.max_len is None or len(name) <= assertion.max_len)
                )
                reason = f"thread name={name!r}; length={len(name)}"
            elif kind == "in_thread":
                passed = (
                    bool(turn.messages)
                    and bool(turn.thread_id)
                    and all(str(m.get("channel_id")) == turn.thread_id for m in turn.messages)
                )
                reason = f"thread={turn.thread_id}"
            elif kind == "same_thread":
                prior = next((t for t in turns if t.number == assertion.as_turn), None)
                if prior is None or not prior.thread_id:
                    raise Pending("reference turn has no observed thread")
                passed = (
                    bool(turn.thread_id)
                    and turn.thread_id == prior.thread_id
                    and all(str(m.get("channel_id")) == prior.thread_id for m in turn.messages)
                )
                reason = f"thread={turn.thread_id}; reference={prior.thread_id}"
            elif kind == "progress_seen":
                passed = turn.progress_seen_s is not None and (
                    turn.progress_seen_s <= (assertion.within_s or 0)
                )
                reason = f"working state observed at {turn.progress_seen_s}s"
            elif kind == "no_blank_message":
                passed = bool(turn.messages) and all(
                    text_of(m, include_fields=False).strip() for m in turn.messages
                )
                reason = "nonblank message content or embed text"
            elif kind == "no_channel_post":
                if not turn.ended_at:
                    raise Pending("parent channel observation was not completed")
                posts = [
                    m
                    for m in turn.parent_messages
                    if m.get("type") != 21
                    and not (
                        m.get("thread")
                        and re.fullmatch(
                            r"Your chat is ready[.!]?",
                            str(m.get("content", "")),
                            re.IGNORECASE,
                        )
                    )
                ]
                passed = not posts
                reason = f"unexpected parent messages: {[m.get('id') for m in posts]}"
            elif kind == "card_finalized":
                passed = bool(turn.messages) and all(
                    backend.classify(m) != "working"
                    and not re.search(r"(?i)working on it|🧠 thinking|⚙️ running tool", text_of(m))
                    for m in turn.messages
                )
                reason = "settled card lifecycle"
            elif kind == "reaction_present":
                passed = any(
                    obj(r.get("emoji")).get("name") == assertion.emoji
                    for r in turn.trigger_reactions
                    + [
                        r
                        for sample in turn.trigger_reaction_history
                        for r in objects(sample.get("reactions"))
                    ]
                    + [r for m in turn.messages for r in objects(m.get("reactions"))]
                )
                reason = f"reaction {assertion.emoji}"
            elif kind == "attachments":
                attachments = [a for m in turn.messages for a in objects(m.get("attachments"))]
                if assertion.name_pattern:
                    attachments = [
                        a
                        for a in attachments
                        if re.search(assertion.name_pattern, str(a.get("filename", "")))
                    ]
                upper_bound_exceeded = (
                    assertion.maximum is not None and len(attachments) > assertion.maximum
                )
                if (
                    not turn.settled
                    and not upper_bound_exceeded
                    and (
                        len(attachments) < assertion.minimum
                        or assertion.maximum is not None
                        or assertion.unique
                    )
                ):
                    raise Pending("turn did not settle; attachment count/uniqueness is unproven")
                passed = len(attachments) >= assertion.minimum and (
                    assertion.maximum is None or len(attachments) <= assertion.maximum
                )
                if assertion.unique:
                    previous = {
                        (a.get("filename"), a.get("size"))
                        for t in turns
                        if t.number < turn.number
                        for m in t.messages
                        for a in objects(m.get("attachments"))
                        if not assertion.name_pattern
                        or re.search(assertion.name_pattern, str(a.get("filename", "")))
                    }
                    keys = [(a.get("filename"), a.get("size")) for a in attachments]
                    passed = (
                        passed and not previous.intersection(keys) and len(set(keys)) == len(keys)
                    )
                reason = f"attachment count={len(attachments)}"
            elif kind in {"log_present", "log_absent"}:
                rows = backend.logs(assertion, turn)
                passed = bool(rows) if kind == "log_present" else not rows
                reason = f"event {assertion.event}: {len(rows)} scoped log entries"
            elif kind == "judge":
                try:
                    passed, reason = judge.evaluate(assertion.rubric or "", turn.text)
                except Pending:
                    raise
                except Exception as exc:
                    judge.errors.append(exception_evidence(exc, "judge"))
                    # A broken evaluator is unavailable evidence, not a
                    # product failure. Never expose provider exception bodies.
                    raise Pending(f"judge execution unavailable: {type(exc).__name__}") from None
            else:
                raise Pending(f"unimplemented assertion: {kind}")
        return Check(kind, "PASS" if passed else "FAIL", reason, assertion.turn, evidence)
    except Pending as exc:
        return Check(kind, "PENDING", str(exc), assertion.turn, evidence)
