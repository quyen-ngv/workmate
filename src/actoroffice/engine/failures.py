"""What kind of failure ended a turn, and what people are told about it (SI-38 BR-6, BR-7).

- network:         the API could not be reached or answered with a server error, or the run stalled (no event for
                   the stage's stall threshold): retried, also after the AI has worked (the session is resumed)
- runtime_start:   the turn failed before doing anything (no token, no event, no tool call; SI-17): retried
- command_timeout: the turn failed right after the office stopped a command that ran past its limit
- ai:              anything else (the AI's own error, an error after it worked): a person looks at it
"""

from __future__ import annotations

import re
from enum import StrEnum


class FailureKind(StrEnum):
    NETWORK = "network"
    RUNTIME_START = "runtime_start"
    COMMAND_TIMEOUT = "command_timeout"
    AI = "ai"


RETRIED = {FailureKind.NETWORK, FailureKind.RUNTIME_START}

NETWORK = re.compile(
    r"unable to connect|certificate|connection (?:error|refused|reset)|(?-i:\b(?:ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENOTFOUND|EAI_AGAIN)\b)"
    r"|fetch failed|socket hang up|network error|overloaded|api error:? 5\d\d|status(?: code)?:? 5\d\d|\b529\b"
    r"|internal server error|service unavailable|bad gateway|gateway timeout",
    re.IGNORECASE,
)
# Windows refused the CLI's command line (too long): the SDK reports it as "Claude Code not found".
COMMAND_LINE_TOO_LONG = re.compile(r"WinError 206|filename or extension is too long", re.IGNORECASE)

LABELS = {
    FailureKind.NETWORK: "mạng/API",
    FailureKind.RUNTIME_START: "lỗi khởi động",
    FailureKind.COMMAND_TIMEOUT: "lệnh quá giờ",
    FailureKind.AI: "lỗi của AI",
}


def classify(error: str | None, *, progressed: bool, stalled: bool = False, command_killed: bool = False) -> FailureKind:
    """The kind of an ERROR turn. `progressed`: the turn used tokens, sent an event or called a tool."""
    if stalled or NETWORK.search(error or ""):
        return FailureKind.NETWORK
    if not progressed:
        return FailureKind.RUNTIME_START
    if command_killed:
        return FailureKind.COMMAND_TIMEOUT
    return FailureKind.AI


def label(kind: str | None) -> str:
    try:
        return LABELS[FailureKind(kind)]
    except ValueError:
        return LABELS[FailureKind.AI]


def hint(kind: str | None, runtime: str, retries: int, error: str | None = None) -> str:
    """What a person needs to do (or that nothing is needed), after the office tried `retries` more times."""
    tried = f"đã thử lại {retries} lần; " if retries else ""
    if kind == FailureKind.NETWORK:
        return f"{tried}kiểm tra kết nối mạng/API rồi bấm Thử lại"
    if kind == FailureKind.RUNTIME_START:
        if COMMAND_LINE_TOO_LONG.search(error or ""):
            return f"{tried}dòng lệnh khởi động {runtime} quá dài (WinError 206), không phải thiếu {runtime}; cần Dev xem"
        return f"{tried}kiểm tra {runtime} trên máy (cài đặt, đăng nhập) rồi bấm Thử lại"
    if kind == FailureKind.COMMAND_TIMEOUT:
        return "cần bạn xem: lệnh của AI chạy quá giới hạn; tăng command_timeout_s trong profile hoặc nhắn AI chạy hẹp hơn"
    return "cần bạn xem tiến độ rồi nhắn hướng đi tiếp hoặc bấm Thử lại"


def run_error(kind: str | None, runtime: str, retries: int, error: str | None) -> str:
    """The error of a failed run (runs.error, and the ticket comment after "Lượt chạy lỗi: ")."""
    return f"{runtime} · {label(kind)}: {hint(kind, runtime, retries, error)}. Chi tiết: {error}"


def retry_title(kind: str | None, runtime: str, attempt: int, of: int, delay_s: float) -> str:
    """The activity line of an automatic retry."""
    return f"{runtime} · {label(kind)}: tự thử lại lần {attempt}/{of} sau {_duration(delay_s)}, không cần làm gì"


def resume_prompt(command: str | None) -> str:
    """The message that resumes the session of a turn cut off by a network/API failure."""
    doing = f" while this command was running: `{command}`" if command else ""
    return (
        f"The previous turn lost its connection to the API (mất kết nối API){doing}. Continue the same work. Before you "
        "run anything again, check the result of your last command or your last edit (it may have finished or been "
        "applied), so you do not repeat it."
    )


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m" if seconds >= 60 and seconds % 60 == 0 else f"{seconds}s"
