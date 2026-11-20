"""VS Code agent adapter (Copilot SDK sessions driven by the editor's agent host).

Layout under the Copilot home (~/.copilot by default):

    session-state/<session_id>/events.jsonl       structured events, appended per turn
    session-state/<session_id>/workspace.yaml     cwd, chat title, client, created_at
    session-state/<session_id>/inuse.<pid>.lock   one file per process holding the session

A session log grows to tens of megabytes, so identity has to come from the sidecar:
workspace.yaml carries the directory, title and start time even when only the tail of
events.jsonl was ever read. Lock file names are the gate for "currently open"; stale
locks of exited processes stay behind, hence the PID check.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field

import config
from core.fs import dirs, homes, project_name
from core.jsonl import JsonlTailer
from core.pid import Liveness
from core.state import (
    AgentSession,
    clip,
    dead_detail,
    dropped,
    first_line,
    fmt_age,
    fmt_duration,
    fmt_tokens,
    hms,
    label_for,
    parse_ts,
)
from .base import AgentSource

_INUSE = re.compile(r"inuse\.(\d+)\.lock$")
_WS_LINE = re.compile(r"^([a-z_]+): ?(.*)$")
_HOME_DIRS = (".copilot",)
_STATE_DIR = "session-state"
_CREDIT_SCAN = 16 * 1024 * 1024
_CREDIT_STEP = 256 * 1024


def _read_from(path: str, start: int, size: int) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            return fh.read(size - start)
    except OSError:
        return None


def _checkpoint_credits(blob: bytes) -> float | None:
    """Credits of the last usage_checkpoint in this region; None when there is none."""
    hit = blob.rfind(b"session.usage_checkpoint")
    if hit < 0:
        return None
    head = blob.rfind(b"\n", 0, hit) + 1
    tail = blob.find(b"\n", hit)
    try:
        record = json.loads(blob[head: tail if tail > 0 else len(blob)])
    except ValueError:
        return None
    snap = (record.get("data") or {}).get("accountingSnapshot")
    if not isinstance(snap, dict):
        return None
    try:
        return float(snap.get("totalPremiumRequestCost") or 0.0)
    except (TypeError, ValueError):
        return None


def _settled(blob: bytes) -> int:
    """Trailing bytes that are still a half-written line."""
    return len(blob) - blob.rfind(b"\n") - 1


def _newest_credits(path: str, size: int) -> tuple[int, float]:
    """(offset to resume from, credits) — scan back in widening steps until a checkpoint turns up."""
    budget = _CREDIT_SCAN
    while True:
        start = max(0, size - budget)
        blob = _read_from(path, start, size)
        if blob is None:
            return size, 0.0
        found = _checkpoint_credits(blob)
        if found is not None or start == 0:
            return size - _settled(blob), found or 0.0
        budget *= 4


@dataclass
class Session:
    key: str
    path: str
    size: float
    mtime: float
    pid: int
    alive: bool
    cwd: str = ""
    title: str = ""
    started_at: float = 0.0


class EventStream:
    """Folds agent-host event records into "what is this session doing now"."""

    def __init__(self) -> None:
        self.pend_tools: dict[str, dict] = {}
        self.pend_perm: dict[str, dict] = {}
        self.turn_open = 0.0
        self.turn_tools = 0
        self.turn_out = 0
        self.compacting = False
        self.ctx_used = 0
        self.ctx_limit = 0
        self.hooks = 0
        self.turns = 0
        self.tool_calls = 0
        self.errors = 0
        self.prompt = ""
        self.model = ""
        self.last_ts = 0.0
        self.turn_failure = ""
        self.turn_stats = ""
        self.activity: deque[str] = deque(maxlen=8)

    def feed(self, records: list[dict]) -> None:
        for ev in records:
            kind = ev.get("type") or ""
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            ts = parse_ts(ev.get("timestamp")) or time.time()
            self.last_ts = max(self.last_ts, ts)
            self._apply(kind, data, ts)

    def _apply(self, kind: str, data: dict, ts: float) -> None:
        if kind == "session.auto_mode_resolved":
            self.model = data.get("chosenModel") or self.model
        elif kind == "session.usage_record":
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            self.model = usage.get("model") or self.model
            # inputTokens of the newest call is the prompt that was just sent, i.e. the
            # live context size; it grows monotonically until a compaction resets it.
            self.turn_out += int(usage.get("outputTokens") or 0)
            self.ctx_used = int(usage.get("inputTokens") or 0) or self.ctx_used
            self.ctx_limit = int(usage.get("maxPromptTokens") or 0) or self.ctx_limit
        elif kind == "user.message":
            self.prompt = first_line(data.get("content"))
            self.turn_failure = ""
        elif kind == "assistant.turn_start":
            self.turns += 1
            self.turn_open = ts
            self.turn_tools = 0
            self.turn_out = 0
        elif kind == "assistant.turn_end":
            steps = self.turn_tools
            span = fmt_duration(max(0.0, ts - self.turn_open)) if self.turn_open else ""
            out = f" / 输出 {fmt_tokens(self.turn_out)}" if self.turn_out else ""
            self.turn_open = 0.0
            self.pend_tools.clear()
            self.pend_perm.clear()
            self.hooks = 0
            self.turn_stats = f"{steps} 工具 / {span}{out}"
        elif kind == "tool.execution_start":
            call_id = str(data.get("toolCallId") or "")
            name = data.get("toolName") or "tool"
            label = label_for(data.get("arguments")) or first_line(data.get("toolTitle"))
            if not call_id:
                return
            if name == "ask_user":
                # A question to the user blocks the turn exactly like a denied tool.
                self.pend_perm[call_id] = {"name": name, "ask": label, "ts": ts}
                return
            self.pend_tools[call_id] = {"name": name, "label": label, "ts": ts}
            self.tool_calls += 1
            self.turn_tools += 1
        elif kind == "tool.execution_complete":
            call_id = str(data.get("toolCallId") or "")
            entry = self.pend_tools.pop(call_id, None) or self.pend_perm.pop(call_id, None) or {}
            name = entry.get("name") or data.get("toolName") or "tool"
            if data.get("success") is False:
                self.errors += 1
                self.activity.append(f"{hms(ts)} {name} 失败")
            else:
                self.activity.append(f"{hms(ts)} {name} ok")
        elif kind == "permission.requested":
            request = data.get("permissionRequest") if isinstance(data.get("permissionRequest"), dict) else {}
            key = str(data.get("requestId") or request.get("toolCallId") or "")
            entry = self.pend_tools.get(str(request.get("toolCallId") or "")) or {}
            if key:
                self.pend_perm[key] = {
                    "name": request.get("kind") or "permission",
                    "ask": request.get("intention") or entry.get("label") or "",
                    "ts": ts,
                }
        elif kind == "permission.completed":
            entry = self.pend_perm.pop(str(data.get("requestId") or ""), None) or {}
            result = str((data.get("result") or {}).get("kind") or "")
            if result.startswith(("deny", "reject")):
                self.activity.append(f"{hms(ts)} 拒绝 {clip(entry.get('ask') or result, 24)}")
        elif kind == "external_tool.requested":
            key = str(data.get("requestId") or data.get("toolCallId") or "")
            if not key:
                return
            self.pend_tools[key] = {
                "name": data.get("toolName") or "mcp",
                "label": label_for(data.get("arguments")),
                "ts": ts,
            }
            self.tool_calls += 1
        elif kind == "external_tool.completed":
            self.pend_tools.pop(str(data.get("requestId") or ""), None)
        elif kind == "hook.start":
            self.hooks += 1
            if data.get("hookType") == "errorOccurred":
                self.turn_failure = "上一轮异常终止"
        elif kind == "hook.end":
            self.hooks = max(0, self.hooks - 1)
        elif kind == "session.compaction_start":
            self.compacting = True
            self.ctx_used = int(data.get("currentTokens") or 0) or self.ctx_used
            self.ctx_limit = int(data.get("tokenLimit") or 0) or self.ctx_limit
        elif kind == "session.compaction_complete":
            self.compacting = False
            if data.get("success") is False:
                self.errors += 1
                self.turn_failure = "上下文压缩失败"
        elif kind == "system.notification":
            info = data.get("kind") if isinstance(data.get("kind"), dict) else {}
            if info.get("type") == "shell_completed":
                code = info.get("exitCode")
                self.activity.append(f"{hms(ts)} 后台命令 {'ok' if code == 0 else f'exit {code}'}")

    def context_pct(self) -> str:
        if not (self.ctx_used and self.ctx_limit):
            return ""
        return f"{self.ctx_used * 100 // self.ctx_limit}%"

    def derive(self, now: float) -> tuple[str, str]:
        if self.pend_perm:
            oldest = min(self.pend_perm.values(), key=lambda e: e["ts"])
            waited = fmt_age(now - oldest["ts"])
            return "awaiting", f"{oldest['name']}: {clip(oldest['ask'], 40)} ({waited})"
        if self.compacting:
            return "compact", "压缩中"
        if self.pend_tools:
            newest = max(self.pend_tools.values(), key=lambda e: e["ts"])
            running = fmt_age(now - newest["ts"])
            return "tool", f"{newest['name']}: {clip(newest['label'], 40)} ({running})"
        if self.turn_open:
            return "model", f"第 {self.turns} 轮请求中 · {fmt_age(now - self.turn_open)}"
        if self.hooks:
            return "hook", f"{self.hooks} 个钩子运行中"
        if self.turn_failure:
            return "error", clip(self.turn_failure, 44)
        if self.turn_stats:
            return "idle", f"上轮 {self.turn_stats} · {fmt_age(now - self.last_ts)}"
        if self.prompt or self.turns:
            return "idle", "等待输入"
        return "starting", "初始化中"

    def has_progress(self) -> bool:
        return bool(self.turns or self.prompt or self.turn_stats or self.pend_tools or self.pend_perm)


@dataclass
class _State:
    tailers: dict[str, JsonlTailer] = field(default_factory=dict)
    streams: dict[str, EventStream] = field(default_factory=dict)
    sidecars: dict[str, tuple] = field(default_factory=dict)
    credits: dict[str, tuple] = field(default_factory=dict)
    liveness: Liveness = field(default_factory=Liveness)


class VscodeAgentSource(AgentSource):
    id = "vscode"
    label = "VS Code"

    def scan(self, cache: dict) -> list[AgentSession]:
        st = cache.get("state") or _State()
        cache["state"] = st
        now = time.time()

        found = self._collect(now, st)
        sessions = []
        for session in found:
            snapshot = self._snapshot(session, now, st)
            if snapshot is not None:
                sessions.append(snapshot)

        live_keys = {s.key for s in found}
        live_paths = {s.path for s in found}
        st.streams = {k: v for k, v in st.streams.items() if k in live_keys}
        st.tailers = {k: v for k, v in st.tailers.items() if k in live_paths}
        return sessions

    # -- discovery -----------------------------------------------------------

    def _collect(self, now: float, st: _State) -> list[Session]:
        found = []

        for home in homes("copilot_homes", ("COPILOT_HOME",), _HOME_DIRS, _STATE_DIR):
            root = os.path.join(home, _STATE_DIR)
            for session_id in dirs(root):
                directory = os.path.join(root, session_id)
                events = os.path.join(directory, "events.jsonl")
                try:
                    info = os.stat(events)
                except OSError:
                    continue
                age = now - info.st_mtime
                if dropped(age):
                    continue
                pid, alive = self._holder(directory, now, st)
                if dropped(age, alive):
                    continue
                sidecar = self._sidecar(directory, st)
                found.append(
                    Session(
                        key=session_id,
                        path=events,
                        size=info.st_size,
                        mtime=info.st_mtime,
                        pid=pid,
                        alive=alive,
                        cwd=sidecar.get("cwd") or "",
                        title=sidecar.get("name") or "",
                        started_at=parse_ts(sidecar.get("created_at")),
                    )
                )
        found.sort(key=lambda s: s.mtime, reverse=True)
        return found

    def _holder(self, directory: str, now: float, st: _State) -> tuple[int, bool]:
        """The process holding the session: newest lock whose PID still lives."""
        locks = []
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    match = _INUSE.match(entry.name)
                    if not match:
                        continue
                    try:
                        mtime = entry.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    locks.append((mtime, int(match.group(1))))
        except OSError:
            return 0, False
        for _, pid in sorted(locks, reverse=True):
            if st.liveness.check(pid, now):
                return pid, True
        return (locks[0][1], False) if locks else (0, False)

    def _sidecar(self, directory: str, st: _State) -> dict:
        path = os.path.join(directory, "workspace.yaml")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return {}
        cached = st.sidecars.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        values: dict[str, str] = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    match = _WS_LINE.match(line.rstrip())
                    if match and match.group(2):
                        values[match.group(1)] = match.group(2).strip().strip('"')
        except OSError:
            values = {}
        if len(st.sidecars) > 200:
            st.sidecars.clear()
        st.sidecars[path] = (mtime, values)
        return values

    # -- per session ---------------------------------------------------------

    def _credits(self, session: Session, st: _State) -> float:
        """Session credits, then only the bytes after the last checkpoint we saw.

        A checkpoint lands minutes and often many megabytes apart, so the tail window
        the state machine reads almost never contains one; tracking a resume offset
        keeps the number live without re-reading, and no checkpoint is ever too far
        behind the end to be found.
        """
        path, size = session.path, int(session.size)
        scanned, total = st.credits.get(path) or (0, 0.0)
        if size < scanned or scanned == 0:
            scanned, total = _newest_credits(path, size)
        elif size - scanned >= _CREDIT_STEP:
            blob = _read_from(path, scanned, size)
            if blob:
                total = max(total, _checkpoint_credits(blob) or 0.0)
                scanned = size - _settled(blob)
        if len(st.credits) > 200:
            st.credits.clear()
        st.credits[path] = (scanned, total)
        return total

    def _usage(self, stream: EventStream, session: Session, st: _State) -> str:
        credits = self._credits(session, st)
        context = stream.context_pct()
        return " · ".join(
            p for p in (f"额度 {credits:g}" if credits else "", f"上下文 {context}" if context else "") if p
        )

    def _snapshot(self, session: Session, now: float, st: _State) -> AgentSession | None:
        tail_bytes = int(config.settings["tail_kb"]) * 1024
        tailer = st.tailers.get(session.path)
        if tailer is None:
            tailer = JsonlTailer(session.path)
            st.tailers[session.path] = tailer
        stream = st.streams.get(session.key)
        if stream is None:
            stream = EventStream()
            st.streams[session.key] = stream

        window = tail_bytes
        if tailer.at_start and session.size <= int(config.settings["full_read_kb"]) * 1024:
            window = None
        records = tailer.read(window)
        if records:
            stream.feed(records)
        if not session.alive and not stream.has_progress():
            return None

        state, detail = stream.derive(now)
        if not session.alive:
            state, detail = dead_detail(now - session.mtime)

        return AgentSession(
            source=self.id,
            key=session.key,
            project=project_name(session.cwd, f"会话 {session.key[:8]}"),
            cwd=session.cwd,
            pid=session.pid,
            alive=session.alive,
            model=stream.model,
            prompt=clip(stream.prompt or session.title, 78),
            state=state,
            detail=detail,
            started_at=session.started_at or session.mtime,
            last_event_at=stream.last_ts or session.mtime,
            turns=stream.turns,
            tool_calls=stream.tool_calls,
            errors=stream.errors,
            usage=self._usage(stream, session, st),
            activity=list(stream.activity)[-3:],
        )
