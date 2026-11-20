"""Source registry. Register one instance per supported agent CLI here."""

from __future__ import annotations

from core.state import AgentSession
from .base import AgentSource
from .opencode import OpencodeSource
from .qoder_cli import QoderCliSource
from .vscode_agent import VscodeAgentSource

SOURCES: list[AgentSource] = [
    QoderCliSource(),
    VscodeAgentSource(),
    OpencodeSource(),
    # CodexCliSource(),   # drop a file in sources/ and add it above
]

_CACHES: dict[str, dict] = {}
ERRORS: dict[str, str] = {}


def scan_all() -> list[AgentSession]:
    sessions: list[AgentSession] = []
    for source in SOURCES:
        cache = _CACHES.setdefault(source.id, {})
        try:
            sessions.extend(source.scan(cache))
            ERRORS.pop(source.id, None)
        except Exception as exc:  # one bad adapter must not blank the board
            ERRORS[source.id] = f"{type(exc).__name__}: {exc}"
    sessions.sort(key=lambda s: s.sort_key())
    return sessions
