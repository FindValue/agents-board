"""The session model every agent-CLI source has to produce.

Adapters own their log parsing; the UI only ever sees AgentSession, so adding a
different CLI means writing a source, not touching board.py.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

import config

# state key -> (label, color, needs the user)
STATE_META: dict[str, tuple[str, str, bool]] = {
    "asking": ("等你回答", "#ff5b5b", True),
    "awaiting": ("等待你授权", "#ff5b5b", True),
    "error": ("上一轮异常", "#ff9f43", True),
    "compact": ("压缩上下文", "#ffd166", False),
    "subagent": ("子代理运行中", "#2dd4bf", False),
    "tool": ("执行工具", "#4bc0ff", False),
    "model": ("请求模型中", "#a78bfa", False),
    "hook": ("运行钩子", "#8894a0", False),
    "starting": ("启动中", "#8894a0", False),
    # Green on purpose: a finished turn waiting for the next prompt is the state you
    # want to spot from across the desk, and grey was invisible at 38% window opacity.
    "idle": ("空闲待输入", "#3ddc97", False),
}


def state_label(state: str) -> str:
    return STATE_META.get(state, (state, "#d7dee6", False))[0]


def state_color(state: str) -> str:
    return STATE_META.get(state, ("", "#d7dee6", False))[1]


def state_attention(state: str) -> bool:
    return STATE_META.get(state, ("", "", False))[2]


@dataclass
class AgentSession:
    source: str
    key: str
    project: str = ""
    cwd: str = ""
    pid: int = 0
    alive: bool = True
    model: str = ""
    branch: str = ""
    prompt: str = ""
    state: str = "starting"
    detail: str = ""
    started_at: float = 0.0
    last_event_at: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    errors: int = 0
    tasks_done: int = 0
    tasks_total: int = 0
    current_task: str = ""
    usage: str = ""
    activity: list[str] = field(default_factory=list)

    @property
    def attention(self) -> bool:
        return state_attention(self.state)

    def sort_key(self) -> tuple:
        """Attention first, then most recently active."""
        return (not self.attention, -self.last_event_at)

    def uptime(self, now: float | None = None) -> float:
        return max(0.0, (now or time.time()) - self.started_at) if self.started_at else 0.0

    def idle_age(self, now: float | None = None) -> float:
        return max(0.0, (now or time.time()) - self.last_event_at) if self.last_event_at else 0.0

    def signature(self) -> str:
        return "|".join(
            (
                self.state,
                self.detail,
                self.prompt,
                self.branch,
                self.model,
                str(self.turns),
                str(self.tool_calls),
                str(self.errors),
                str(self.tasks_done),
                str(self.tasks_total),
                self.current_task,
                self.usage,
                str(len(self.activity)),
                self.activity[-1] if self.activity else "",
                str(int(self.idle_age() / 5)),
                str(int(self.uptime() / 30)),
                str(self.alive),
            )
        )


def dropped(age: float, alive: bool | None = None) -> bool:
    """Whether a session leaves the board, given how long since it last wrote anything.

    Discovery asks the cheap question first (no `alive` yet, so a months-old run is
    filtered without spending a liveness check on its pid), then the same call with the
    verdict applies the just-exited rule. Both budgets are settings, not adapter policy.
    """
    silence = config.settings["max_idle_minutes"] * 60
    recent_dead = config.settings["recent_dead_minutes"] * 60
    if age > max(silence, recent_dead):
        return True
    if alive is not None and not alive:
        return not (config.settings["include_recent_dead"] and age < recent_dead)
    return False


def dead_detail(age: float) -> tuple[str, str]:
    """A session whose process is gone reads as finished work, never as a live state."""
    return "idle", f"进程已退出 · 最后活动 {fmt_age(age)}"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def fmt_age(seconds: float) -> str:
    if seconds < 5:
        return "刚刚"
    if seconds < 60:
        return f"{int(seconds)}s 前"
    if seconds < 3600:
        return f"{int(seconds / 60)}m 前"
    return f"{int(seconds / 3600)}h 前"


def fmt_tokens(count: int) -> str:
    """Token counts only matter in bulk; keep the digits that carry information."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 10_000:
        return f"{count // 1000}k"
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def parse_ts(value) -> float:
    """ISO-8601 (also the trailing-Z form) to epoch seconds; 0.0 when unusable."""
    if not isinstance(value, str):
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def hms(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "--:--"


def first_line(text) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(text.strip().splitlines()[:1]) or text[:60]


_LABEL_KEYS = (
    "description",
    "command",
    "prompt",
    "url",
    "file_path",
    "path",
    "pattern",
    "subject",
    "message",
    "question",
)


def label_for(payload) -> str:
    """Short human label for a tool payload, whatever shape the CLI passes."""
    if isinstance(payload, str):
        return first_line(payload)
    if not isinstance(payload, dict):
        return ""
    # AskUserQuestion / question tools carry a list of questions, which defeats the
    # flat key scan; both CLIs use the same {questions: [{question|header, ...}]} shape.
    questions = payload.get("questions")
    if isinstance(questions, list):
        texts = [
            str(q.get("question") or q.get("header") or "") for q in questions if isinstance(q, dict)
        ]
        texts = [t for t in texts if t.strip()]
        if texts:
            return texts[0] if len(texts) == 1 else f"{texts[0]} 等 {len(texts)} 问"
    for key in _LABEL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def display_width(text: str) -> int:
    """Terminal-ish width: CJK and fullwidth glyphs take two cells."""
    total = 0
    for ch in text:
        code = ord(ch)
        if (
            0x1100 <= code <= 0x115F
            or 0x2E80 <= code <= 0xA4CF
            or 0xAC00 <= code <= 0xD7A3
            or 0xF900 <= code <= 0xFAFF
            or 0xFE30 <= code <= 0xFE6F
            or 0xFF00 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6
            or 0x20000 <= code <= 0x3FFFD
        ):
            total += 2
        else:
            total += 1
    return total


def clip(text: str, budget: int, unit=display_width) -> str:
    """Ellipsize to a budget. `unit` says how wide something is: cell counts by
    default, a font's measure() when the caller has a pixel box to fit."""
    text = " ".join(str(text or "").split())
    if unit(text) <= budget:
        return text
    return _take(text, max(1, budget - unit("…")), unit) + "…"


def _take(text: str, budget: int, unit=display_width) -> str:
    out = []
    used = 0
    for ch in text:
        w = unit(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out)
