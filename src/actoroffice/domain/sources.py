"""Sources of a ticket (SI-39): what its description points to, found without any I/O.

`parse_refs` only proposes candidates. Whether `XX-n` is a ticket or a Jira key is decided by the service with the
office's data (a ticket that exists, or the Jira project key of a team): no fixed list of strings to ignore.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'`]+")
URL_TAIL = ".,;:!?*_"
# A relative path with a folder and an extension, then an optional place in it kept as written: §5.5 · :120 · :120-130 · #L10 · #L10-L20
FILE_RE = re.compile(
    r"(?<![\w/.:@\\-])((?:[\w.-]+/)+[\w-]+(?:\.[\w-]+)*\.[A-Za-z0-9]+)(?![\w/])"
    r"(?:[ \t]?(§[ \t]?\d+(?:\.\d+)*)|(:\d+(?:-\d+)?)(?![\w:])|(#L\d+(?:-L?\d+)?)(?!\w))?"
)
CODE_RE = re.compile(r"(?<![\w/.-])([A-Z][A-Z0-9]*)-(\d+)(?![\w-])")

KINDS = ("ticket", "jira", "file", "url", "comment")
STATUS_LABEL = {
    "read": "đã đọc",
    "unread": "chưa đọc",
    "skipped": "không liên quan",
    "error": "không đọc được (lỗi)",
    "external": "không đọc (URL ngoài)",
    "out_of_scope": "ngoài phạm vi",
}


@dataclass(frozen=True)
class RawRef:
    kind: str  # "code" (ticket or Jira key, decided later) | "file" | "url"
    ref: str
    locator: str
    pos: int


def parse_refs(text: str) -> list[RawRef]:
    """Candidates in the order they appear; each (kind, ref, locator) once. A URL hides the paths and keys inside it,
    a path hides the keys inside it."""
    found: list[RawRef] = []
    taken: list[tuple[int, int]] = []
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(URL_TAIL)
        found.append(RawRef("url", url, "", m.start()))
        taken.append((m.start(), m.end()))
    masked = _mask(text or "", taken)
    for m in FILE_RE.finditer(masked):
        path = m.group(1).removeprefix("./")
        locator = (m.group(2) or m.group(3) or m.group(4) or "").replace(" ", "").replace("\t", "")
        found.append(RawRef("file", path, locator, m.start()))
        taken.append((m.start(), m.end()))
    masked = _mask(text or "", taken)
    for m in CODE_RE.finditer(masked):
        found.append(RawRef("code", m.group(0), "", m.start()))
    seen: set[tuple[str, str, str]] = set()
    unique: list[RawRef] = []
    for ref in sorted(found, key=lambda r: r.pos):
        key = (ref.kind, ref.ref, ref.locator)
        if key not in seen:
            seen.add(key)
            unique.append(ref)
    return unique


def display(kind: str, ref: str, locator: str) -> str:
    """How a source is written for people: the locator stays next to the path (`docs/x.md §5.5`, `src/a.py:120`)."""
    if not locator:
        return ref
    return f"{ref} {locator}" if locator.startswith("§") else f"{ref}{locator}"


def first_line(locator: str) -> int | None:
    """The line a file locator points to (`:120`, `:120-130`, `#L10-L20`), None for a section or nothing."""
    m = re.match(r"^(?::|#L)(\d+)", locator or "")
    return int(m.group(1)) if m else None


def line_range(locator: str) -> tuple[int, int] | None:
    m = re.match(r"^(?::|#L)(\d+)(?:-L?(\d+))?$", locator or "")
    if not m:
        return None
    start = int(m.group(1))
    return start, int(m.group(2) or start)


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            chars[i] = " "
    return "".join(chars)
