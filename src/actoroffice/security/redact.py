"""Mask secrets before anything is logged, stored as an event, or sent to a channel."""

from __future__ import annotations

import re

MASK = "‹redacted›"

PATTERNS = [
    re.compile(p)
    for p in (
        r"sk-ant-[A-Za-z0-9_\-]{20,}",
        r"sk-[A-Za-z0-9]{20,}",
        r"gh[pousr]_[A-Za-z0-9]{30,}",
        r"xox[abprs]-[A-Za-z0-9\-]{10,}",
        r"AKIA[0-9A-Z]{16}",
        r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b",  # Telegram bot token
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----",
        r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._\-]{16,}",
    )
]


class Redactor:
    def __init__(self) -> None:
        self._known: set[str] = set()

    def register(self, secret: str | None) -> None:
        """Every secret value loaded from the keyring is registered here."""
        if secret and len(secret) >= 6:
            self._known.add(secret)

    def __call__(self, text: str) -> str:
        if not text:
            return text
        for secret in sorted(self._known, key=len, reverse=True):
            text = text.replace(secret, MASK)
        for pattern in PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + MASK, text)
        return text
