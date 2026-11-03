"""Decode immutable email evidence without trusting model-supplied message text."""

import base64
import re
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from typing import cast


@dataclass(frozen=True)
class EmailEvidence:
    text: str
    recipients: frozenset[str]


def decode_email(raw: str) -> EmailEvidence:
    if len(raw) > 20_000_000:
        raise ValueError("source_too_large")
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    )
    headers = "\n".join(f"{key}: {value}" for key, value in message.items())
    parts: list[str] = []

    def collect(part: Message) -> None:
        # Attachment containers must prune their whole subtree, including .eml
        # bodies whose descendants have no disposition or filename themselves.
        if (
            part.get_content_disposition() == "attachment"
            or part.get_filename() is not None
            or part.get_content_maintype() == "message"
        ):
            return
        if part.is_multipart():
            children = part.get_payload()
            if isinstance(children, list):
                for child in cast("list[object]", children):
                    if isinstance(child, Message):
                        collect(cast("Message", child))
            return
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                parts.append(
                    payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                )

    collect(message)
    recipients = frozenset(
        address.casefold()
        for _, address in getaddresses(
            [
                str(value)
                for header in ("To", "Cc", "Delivered-To", "X-Original-To")
                for value in message.get_all(header, [])
            ]
        )
    )
    return EmailEvidence(text=headers + "\n\n" + "\n\n".join(parts), recipients=recipients)


def has_registration_request(content: str) -> bool:
    """Require a direct lead-registration request at the start of a user's message."""
    return (
        re.match(
            r"\s*(?:<@!?\d+>\s*)?(?:(?:please|can you|could you|would you)\s+)?"
            r"register this lead\b",
            content,
            flags=re.IGNORECASE,
        )
        is not None
    )
