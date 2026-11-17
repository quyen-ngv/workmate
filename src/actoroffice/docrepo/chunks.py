"""A Markdown file cut into the pieces the full-text index holds: one per heading, each knowing the headings above it
and the line it starts on, so a search hit says where to read. No I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .layout import body_offset, split

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")
FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
MAX_CHARS = 2400  # a piece longer than this is cut at a blank line: a hit should be a screenful, not a chapter


@dataclass(frozen=True)
class Chunk:
    heading: str  # "Design — AO-13 › Thay đổi › C-2 [mới]: bảng hạn mức"
    line: int  # 1-based line of the file the piece starts on
    text: str


def chunks(text: str) -> list[Chunk]:
    """The pieces of one file. The front matter is not indexed; line numbers are those of the whole file."""
    _, body = split(text)
    first = body_offset(text) + 1
    out: list[Chunk] = []
    trail: list[tuple[int, str]] = []  # the headings we are under: (level, title)
    start, lines, fenced = first, [], False

    def close() -> None:
        piece = "\n".join(lines).strip()
        if piece:
            heading = " › ".join(title for _, title in trail)
            for offset, part in _cut(piece):
                out.append(Chunk(heading, start + offset, part))

    for number, line in enumerate(body.split("\n"), first):
        if FENCE_RE.match(line):
            fenced = not fenced
        heading = None if fenced else HEADING_RE.match(line)
        if heading:
            close()
            level = len(heading.group(1))
            while trail and trail[-1][0] >= level:
                trail.pop()
            trail.append((level, heading.group(2).strip()))
            start, lines = number, [line]
        else:
            lines.append(line)
    close()
    return out


def _cut(piece: str) -> list[tuple[int, str]]:
    """(line offset, text) parts of a piece, none much longer than MAX_CHARS, cut at blank lines."""
    if len(piece) <= MAX_CHARS:
        return [(0, piece)]
    parts: list[tuple[int, str]] = []
    current: list[str] = []
    begin = size = 0
    for offset, line in enumerate(piece.split("\n")):
        if size > MAX_CHARS and not line.strip() and current:
            parts.append((begin, "\n".join(current).strip()))
            current, size, begin = [], 0, offset + 1
            continue
        current.append(line)
        size += len(line) + 1
    if "\n".join(current).strip():
        parts.append((begin, "\n".join(current).strip()))
    return parts
