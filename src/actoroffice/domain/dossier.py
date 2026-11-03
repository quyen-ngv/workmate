"""The ticket dossier (document `dossier`): fixed sections under `## <title>` headings, and the one line the office
keeps in its tests section: how far the ticket's branch was brought up to date with the main branch (SI-21)."""

from __future__ import annotations

import re

DOSSIER_SECTIONS = {
    "context": "Bối cảnh",
    "requirements": "Yêu cầu & AC",
    "qa": "Hỏi đáp",
    "decisions": "Quyết định",
    "technical": "Kỹ thuật",
    "access": "Truy cập môi trường",
    "tests": "Kiểm thử",
    "deviations": "Lệch kế hoạch",
    "links": "Liên kết",
}
SYNC_SECTION = "tests"
SYNC_LINE = re.compile(r"(?m)^[ \t]*Đã cập nhật theo \S+ tới [0-9a-f]{7,40} \(\d{4}-\d{2}-\d{2}\)[ \t]*$")


def parse_dossier(text: str) -> dict[str, str]:
    by_title = {v.lower(): k for k, v in DOSSIER_SECTIONS.items()}
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and (m.group(1).lower() in by_title or m.group(1).lower() in DOSSIER_SECTIONS):
            if current:
                sections[current] = "\n".join(buf).strip()
            current = by_title.get(m.group(1).lower(), m.group(1).lower())
            buf = []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return sections


def render_dossier(sections: dict[str, str]) -> str:
    parts = ["# Dossier"]
    for key, title in DOSSIER_SECTIONS.items():
        if sections.get(key, "").strip():
            parts.append(f"## {title}\n\n{sections[key].strip()}")
    return "\n\n".join(parts) + "\n"


def sync_line(main: str, sha: str, day: str) -> str:
    """The office's line: `Đã cập nhật theo <main> tới <sha7> (<YYYY-MM-DD>)`."""
    return f"Đã cập nhật theo {main} tới {sha[:7]} ({day})"


def set_sync_line(section: str, line: str) -> str:
    """The section with exactly one office line, `line`, at its end (an older line is replaced)."""
    rest = SYNC_LINE.sub("", section).strip()
    rest = re.sub(r"\n{3,}", "\n\n", rest)
    return f"{rest}\n\n{line}" if rest else line


def keep_sync_line(old: str, new: str) -> str:
    """`new` overwrites `old`; the office line of `old` stays (at the end) when `new` has none of its own."""
    found = SYNC_LINE.search(old)
    if found is None or SYNC_LINE.search(new):
        return new
    return set_sync_line(new, found.group(0).strip())
