"""Qoder CLI adapter.

Layout under the qoder home (~/.qoder-cn by default):

    logs/sessions/<project_id>/<session_id>/segments/<run_id>.jsonl   structured events
    logs/runs/<run_id>/manifest.json                                  pid, cwd, cli_version
    projects/<project_id>/<session_id>.jsonl                          transcript (tasks, branch)

The run id ends in ``-p<pid>``, so process liveness is the gate for "currently
open". Events are the source of truth for what the session is doing right now.

The Qoder CN desktop worker (``--sdk``) writes the same layout, but one run id is
shared by every open conversation and the manifest cwd is the app install dir; there
each session directory is its own card and its log mtime says which ones are current.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import config
from core import i18n
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
    hms,
    label_for,
    parse_ts,
)
from .base import AgentSource

_RUN_PID = re.compile(r"-p(\d+)$")
_TASK_CREATED = re.compile(r"Task #(\d+) created successfully:?\s*(.*)")
_PROMPT_NOISE = re.compile(r"^<(local-command-caveat|command-message|command-name|system-reminder)")
_HOME_DIRS = (".qoder-cn", ".qoder")
_SESSIONS_DIR = os.path.join("logs", "sessions")
_COMPACT_LABEL = {
    "starting": "compact_starting",
    "summarizing": "compact_summarizing",
    "post_processing": "compact_post_processing",
}


def _prompt_text(rec: dict) -> str:
    """Text of a real user request; "" for tool results and injected notices."""
    if rec.get("type") != "user":
        return ""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = next(
            (b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"), ""
        )
    else:
        return ""
    text = text.strip()
    return "" if _PROMPT_NOISE.match(text) else text


def _run_start(run_id: str) -> float:
    try:
        return datetime.strptime(run_id[:19], "%Y-%m-%dT%H-%M-%S").timestamp()
    except ValueError:
        return 0.0


@dataclass
class Run:
    key: str
    path: str
    run_id: str
    project_id: str
    pid: int
    mtime: float
    alive: bool
    size: float = 0.0
    cwd: str = ""
    model: str = ""
    started_at: float = 0.0
    sdk: bool = False


class SegmentStream:
    """Folds qodercli event records into "what is this session doing now"."""

    def __init__(self) -> None:
        self.pend_tools: dict[str, dict] = {}
        self.pend_perm: dict[str, dict] = {}
        self.pend_ask: dict[str, dict] = {}
        self.pend_fork: dict[str, float] = {}
        self.request: tuple[int, float] | None = None
        self.retry: tuple[int, int] | None = None
        self.compact_stage: str | None = None
        self.hooks = 0
        self.turns = 0
        self.tool_calls = 0
        self.errors = 0
        self.prompt = ""
        self.model = ""
        self.cwd = ""
        self.transcript = ""
        self.last_ts = 0.0
        self.last_type = ""
        self.active_turns: set[str] = set()
        self.turn_failure = ""
        self.last_error = ""
        self.turn_stats = ""
        self.activity: deque[str] = deque(maxlen=8)

    def feed(self, records: list[dict]) -> None:
        for ev in records:
            kind = ev.get("type") or ""
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            ts = parse_ts(ev.get("ts")) or time.time()
            self.last_ts = max(self.last_ts, ts)
            self.last_type = kind
            tcid = ev.get("tool_call_id") or ""
            self._apply(kind, data, ts, tcid, ev.get("level"), ev.get("turn_id") or "")

    def _apply(
        self, kind: str, data: dict, ts: float, tcid: str, level: str | None, turn_id: str
    ) -> None:
        if kind == "session.config.loaded":
            self.model = data.get("model") or self.model
        elif kind == "input.prompt.received":
            self.prompt = data.get("text_preview") or ""
            self.turn_stats = ""
            self.turn_failure = ""
            self.active_turns.add(turn_id)
        elif kind == "turn.started":
            self.turns += 1
            self.model = data.get("model") or self.model
            self.turn_failure = ""
            self.turn_stats = ""
            self.retry = None
            self.active_turns.add(turn_id)
        elif kind == "model.request.started":
            self.request = (int(data.get("request_index") or 1), ts)
        elif kind == "model.response.completed":
            self.request = None
            self.retry = None
        elif kind == "model.request.attempt_failed":
            self.errors += 1
            self.retry = (int(data.get("attempt") or 1), int(data.get("max_attempts") or 1))
            self.last_error = clip(data.get("error_message") or i18n.t("model_request_failed"), 70)
        elif kind == "tool.requested":
            name = data.get("tool_name") or "tool"
            if tcid:
                # permission.requested for this tool is only written once the user has
                # answered, so the wait itself is visible exclusively as a pending tool.
                if name == "AskUserQuestion":
                    self.pend_ask[tcid] = {"text": label_for(data.get("args") or {}), "ts": ts}
                self.pend_tools[tcid] = {
                    "name": name,
                    "label": label_for(data.get("args") or {}),
                    "ts": ts,
                }
                self.tool_calls += 1
        elif kind == "permission.requested":
            if tcid:
                self.pend_perm[tcid] = {
                    "name": data.get("tool_name") or "tool",
                    "ask": data.get("ask_message") or label_for(data.get("args") or {}),
                    "ts": ts,
                }
        elif kind == "permission.resolved":
            entry = self.pend_perm.pop(tcid, None)
            if data.get("decision_behavior") == "deny":
                self.activity.append(
                    f"{hms(ts)} {i18n.t('act_denied', name=data.get('tool_name') or (entry or {}).get('name', ''))}"
                )
        elif kind == "tool.shell.started":
            entry = self.pend_tools.get(tcid)
            if entry:
                entry["label"] = first_line(data.get("command") or entry["label"])
        elif kind == "tool.shell.backgrounded":
            self.activity.append(f"{hms(ts)} {i18n.t('act_backgrounded', pid=data.get('pid'))}")
        elif kind == "tool.shell.finished":
            entry = self.pend_tools.get(tcid) or {}
            code = data.get("exit_code")
            mark = "ok" if code == 0 else f"exit {code}"
            if data.get("aborted"):
                mark = i18n.t("act_aborted")
            self.activity.append(f"{hms(ts)} {entry.get('name', 'shell')} {mark}")
        elif kind == "tool.execution.finished":
            entry = self.pend_tools.pop(tcid, None) or {}
            self.pend_ask.pop(tcid, None)
            status = data.get("status")
            name = entry.get("name") or data.get("tool_name") or "tool"
            if status == "error":
                self.errors += 1
                self.activity.append(f"{hms(ts)} {name} {i18n.t('act_failed')}")
            elif status == "denied":
                self.activity.append(f"{hms(ts)} {i18n.t('act_tool_denied', name=name)}")
            elif "shell" not in (self.activity[-1] if self.activity else ""):
                self.activity.append(f"{hms(ts)} {name} ok")
        elif kind == "fork.agent.started":
            self.pend_fork[str(data.get("fork_label") or "agent")] = ts
        elif kind == "fork.agent.completed":
            label = str(data.get("fork_label") or "agent")
            self.pend_fork.pop(label, None)
            if not data.get("success", True):
                self.errors += 1
        elif kind == "compression.progress":
            stage = data.get("stage")
            self.compact_stage = None if stage in ("done", "failed") else stage
        elif kind == "hook.started":
            self.hooks += 1
            # The desktop worker's manifest cwd is the app install dir; hook_input is
            # the one place its real workspace path is written down.
            hook_input = data.get("hook_input") if isinstance(data.get("hook_input"), dict) else {}
            if isinstance(hook_input.get("cwd"), str) and hook_input["cwd"]:
                self.cwd = hook_input["cwd"]
        elif kind == "hook.finished":
            self.hooks = max(0, self.hooks - 1)
        elif kind == "input.slash_command.started":
            self.activity.append(f"{hms(ts)} /{data.get('command_name')}")
        elif kind == "session.phase.finished":
            if data.get("phase") == "transcript.writer.initialized":
                path = data.get("path")
                if isinstance(path, str):
                    self.transcript = path
        elif kind == "turn.finished":
            reason = data.get("reason") or "end_turn"
            self.active_turns.discard(turn_id)
            self.request = None
            self.pend_tools.clear()
            self.pend_perm.clear()
            self.pend_ask.clear()
            self.pend_fork.clear()
            self.hooks = 0
            steps = data.get("num_turns") or self.turns
            dur = fmt_duration((data.get("duration_ms") or 0) / 1000.0)
            self.turn_stats = i18n.t("turn_stats_steps", steps=steps, duration=dur)
            if reason == "abort":
                self.turn_failure = i18n.t("turn_failure_interrupted")
            elif reason == "error" or level == "error":
                self.turn_failure = i18n.t("turn_failure_error", reason=reason)
        elif kind == "loop.iteration.finished":
            if data.get("error_tool_result_count"):
                self.errors += int(data["error_tool_result_count"])

    @property
    def in_turn(self) -> bool:
        # The desktop worker runs a main turn and skill-review subagent turns at the
        # same time; one turn's finish must not hide another still generating.
        return bool(self.active_turns)

    def derive(self, now: float) -> tuple[str, str]:
        if self.pend_ask:
            oldest = min(self.pend_ask.values(), key=lambda e: e["ts"])
            waited = fmt_age(now - oldest["ts"])
            return "asking", f"{clip(oldest['text'], 40)} ({waited})"
        if self.pend_perm:
            oldest = min(self.pend_perm.values(), key=lambda e: e["ts"])
            waited = fmt_age(now - oldest["ts"])
            return "awaiting", f"{oldest['name']}: {clip(oldest['ask'], 40)} ({waited})"
        if self.compact_stage:
            return "compact", i18n.t(_COMPACT_LABEL.get(self.compact_stage, self.compact_stage))
        if self.pend_fork:
            labels = "/".join(sorted(self.pend_fork))
            return "subagent", i18n.t("detail_forks", n=len(self.pend_fork), labels=clip(labels, 30))
        if self.pend_tools:
            newest = max(self.pend_tools.values(), key=lambda e: e["ts"])
            running = fmt_age(now - newest["ts"])
            return "tool", f"{newest['name']}: {clip(newest['label'], 40)} ({running})"
        if self.request:
            index, started = self.request
            extra = i18n.t("detail_retry", a=self.retry[0], b=self.retry[1]) if self.retry else ""
            return "model", i18n.t("detail_model_request", n=index, extra=extra, age=fmt_age(now - started))
        if self.hooks:
            return "hook", i18n.t("detail_hooks", n=self.hooks)
        if self.turn_failure:
            return "error", clip(self.turn_failure, 44)
        # A turn can be quiet between events; the last turn's stats must not make an
        # in-flight request read as "waiting for input".
        if self.in_turn and (self.prompt or self.turns):
            return "model", i18n.t("detail_model", age=fmt_age(now - self.last_ts))
        if self.turn_stats:
            return "idle", i18n.t("detail_prev", stats=self.turn_stats, age=fmt_age(now - self.last_ts))
        if self.last_error and now - self.last_ts < 120:
            return "error", self.last_error
        if self.prompt or self.turns:
            return "idle", i18n.t("detail_waiting")
        return "starting", i18n.t("detail_starting")

    def has_progress(self) -> bool:
        return bool(self.turns or self.prompt or self.turn_stats or self.pend_tools or self.pend_perm)


class TranscriptFeed:
    """Task checklist and git branch, which only exist in the transcript."""

    def __init__(self) -> None:
        self.tasks: dict[str, list] = {}
        self.branch = ""
        self._creates: dict[str, str] = {}

    def feed(self, records: list[dict]) -> None:
        for rec in records:
            branch = rec.get("gitBranch")
            if isinstance(branch, str) and branch and branch != "HEAD":
                self.branch = branch
            if _prompt_text(rec):
                # A checklist describes the request that built it: once every item is
                # done, the next prompt turns it into history.
                if self.tasks and all(status == "completed" for _, status in self.tasks.values()):
                    self.tasks.clear()
            content = (rec.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name") or ""
                    inp = block.get("input") or {}
                    if name in ("TaskCreate", "TaskUpdate", "TodoWrite") or name.startswith("Todo"):
                        self._tool_use(str(block.get("id") or ""), name, inp)
                elif block.get("type") == "tool_result":
                    self._tool_result(str(block.get("tool_use_id") or ""), block.get("content"))

    def _tool_use(self, use_id: str, name: str, inp: dict) -> None:
        if not isinstance(inp, dict):
            return
        if name == "TaskCreate":
            self._creates[use_id] = str(inp.get("subject") or inp.get("description") or "")
        elif name == "TaskUpdate":
            tid = str(inp.get("taskId") or inp.get("task_id") or "")
            status = inp.get("status")
            if tid and status:
                self.tasks.setdefault(tid, [i18n.t("task_fallback", tid=tid), "pending"])[1] = status
        elif name == "TodoWrite":
            todos = inp.get("todos")
            if isinstance(todos, list):
                self.tasks = {
                    str(i): [str(t.get("content") or t.get("activeForm") or ""), str(t.get("status") or "pending")]
                    for i, t in enumerate(todos)
                    if isinstance(t, dict)
                }

    def _tool_result(self, use_id: str, content) -> None:
        # Only a result for a TaskCreate we saw counts: agent output frequently
        # quotes this very log line, and that must not register as a task.
        fallback = self._creates.pop(use_id, None)
        if fallback is None:
            return
        text = content if isinstance(content, str) else ""
        if not text and isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        match = _TASK_CREATED.search(text)
        if not match:
            return
        tid, subject = match.group(1), match.group(2).strip().rstrip('"]')
        self.tasks[tid] = [subject or fallback or i18n.t("task_fallback", tid=tid), "pending"]

    def progress(self) -> tuple[int, int, str]:
        done = sum(1 for v in self.tasks.values() if v[1] == "completed")
        current = ""
        for tid, (subject, status) in self.tasks.items():
            if status == "in_progress":
                current = f"#{tid} {subject}"
                break
        if not current and self.tasks:
            for tid, (subject, status) in self.tasks.items():
                if status == "pending":
                    current = i18n.t("task_next", tid=tid, subject=subject)
                    break
        return done, len(self.tasks), current


@dataclass
class _State:
    tailers: dict[str, JsonlTailer] = field(default_factory=dict)
    note_tailers: dict[str, JsonlTailer] = field(default_factory=dict)
    streams: dict[str, SegmentStream] = field(default_factory=dict)
    feeds: dict[str, TranscriptFeed] = field(default_factory=dict)
    manifests: dict[str, tuple] = field(default_factory=dict)
    segments: dict[str, tuple[int, list[str]]] = field(default_factory=dict)
    liveness: Liveness = field(default_factory=Liveness)


class QoderCliSource(AgentSource):
    id = "qoder"
    label = "Qoder CLI"

    def scan(self, cache: dict) -> list[AgentSession]:
        st = cache.get("state") or _State()
        cache["state"] = st
        now = time.time()

        runs = self._collect_runs(now, st)
        sessions = []
        for run in runs:
            session = self._snapshot(run, now, st)
            if session is not None:
                sessions.append(session)

        live_keys = {r.key for r in runs}
        live_paths = {r.path for r in runs}
        st.streams = {k: v for k, v in st.streams.items() if k in live_keys}
        st.tailers = {k: v for k, v in st.tailers.items() if k in live_paths}
        return sessions

    # -- discovery -----------------------------------------------------------

    def _collect_runs(self, now: float, st: _State) -> list[Run]:
        runs = []

        for home in homes("qoder_homes", ("QODER_CLI_HOME", "QODER_HOME"), _HOME_DIRS, _SESSIONS_DIR):
            root = os.path.join(home, _SESSIONS_DIR)
            for project_id in dirs(root):
                for session_id in dirs(os.path.join(root, project_id)):
                    newest = _newest_segment(os.path.join(root, project_id, session_id, "segments"), st)
                    if not newest:
                        continue
                    path, mtime, size, run_id = newest
                    age = now - mtime
                    if dropped(age):
                        continue
                    match = _RUN_PID.search(run_id)
                    if not match:
                        continue
                    pid = int(match.group(1))
                    meta = self._manifest(home, run_id, st)
                    sdk = "--sdk" in (meta.get("argv") or [])
                    if sdk:
                        # One desktop worker owns every open conversation, so its pid alone
                        # cannot say which ones are still on screen; each log's own mtime can.
                        alive = st.liveness.check(pid, now) and age < config.settings["live_minutes"] * 60
                        cwd = ""
                    else:
                        alive = st.liveness.check(pid, now)
                        cwd = meta.get("cwd") or ""
                    if dropped(age, alive):
                        continue
                    runs.append(
                        Run(
                            key=f"{project_id}/{session_id}/{run_id}",
                            path=path,
                            run_id=run_id,
                            project_id=project_id,
                            pid=pid,
                            mtime=mtime,
                            alive=alive,
                            size=size,
                            cwd=cwd,
                            model=meta.get("model") or "",
                            started_at=parse_ts(meta.get("started_at")) or _run_start(run_id),
                            sdk=sdk,
                        )
                    )
        runs.sort(key=lambda r: r.mtime, reverse=True)
        return runs

    def _manifest(self, home: str, run_id: str, st: _State) -> dict:
        path = os.path.join(home, "logs", "runs", run_id, "manifest.json")
        cached = st.manifests.get(run_id)
        try:
            mtime = os.path.getsize(path)
        except OSError:
            if cached:
                return cached[1]
            st.manifests[run_id] = (0, {})
            return {}
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if data.get("process_role") != "main" or data.get("env_flags", {}).get("remote_worker"):
            data = {}
        st.manifests[run_id] = (mtime, data)
        return data

    # -- per session ---------------------------------------------------------

    def _transcript(self, stream: SegmentStream, st: _State, tail_bytes: int) -> TranscriptFeed | None:
        """Task checklist + branch come from the transcript, not the event log."""
        path = stream.transcript
        if not path:
            return None
        feed = st.feeds.get(path)
        if feed is None:
            if len(st.feeds) > 60:
                st.feeds.clear()
                st.note_tailers.clear()
            feed = TranscriptFeed()
            st.feeds[path] = feed
            st.note_tailers[path] = JsonlTailer(path)
        note_t = st.note_tailers[path]
        window = None if (note_t.at_start and _small(path)) else tail_bytes
        notes = note_t.read(window)
        if notes:
            feed.feed(notes)
        return feed

    def _snapshot(self, run: Run, now: float, st: _State) -> AgentSession | None:
        tail_bytes = int(config.settings["tail_kb"]) * 1024
        tailer = st.tailers.get(run.path)
        if tailer is None:
            tailer = JsonlTailer(run.path)
            st.tailers[run.path] = tailer
        stream = st.streams.get(run.key)
        if stream is None:
            stream = SegmentStream()
            st.streams[run.key] = stream

        # First look at a file starts from its head when we can afford it: session
        # identity (model, transcript path, first prompt) only appears at the top.
        window = tail_bytes
        if tailer.at_start and run.size <= int(config.settings["full_read_kb"]) * 1024:
            window = None
        records = tailer.read(window)
        if records:
            stream.feed(records)
        if not run.alive and not stream.has_progress():
            return None
        # The worker also writes title-generation and hook-only slices; neither is a
        # conversation worth a card.
        if run.sdk and (not stream.has_progress() or stream.prompt.startswith("Session title:")):
            return None

        cwd = stream.cwd or run.cwd
        project = project_name(
            cwd, run.project_id.replace("--", "\\").split("\\")[-1] or run.project_id
        )
        feed = self._transcript(stream, st, tail_bytes)

        state, detail = stream.derive(now)
        if not run.alive:
            state, detail = dead_detail(now - run.mtime)
        done, total, current = feed.progress() if feed else (0, 0, "")
        last_event = stream.last_ts or run.mtime

        return AgentSession(
            source=self.id,
            key=run.key,
            project=project,
            cwd=cwd,
            pid=run.pid,
            alive=run.alive,
            model=stream.model or run.model,
            branch=feed.branch if feed else "",
            prompt=clip(stream.prompt, 78),
            state=state,
            detail=detail,
            started_at=run.started_at,
            last_event_at=last_event,
            turns=stream.turns,
            tool_calls=stream.tool_calls,
            errors=stream.errors,
            tasks_done=done,
            tasks_total=total,
            current_task=clip(current, 60) if current else "",
            activity=list(stream.activity)[-3:],
        )


def _segment_names(segments_dir: str, st: _State) -> list[str]:
    """The .jsonl files of one session, re-listed only when the directory gained or lost one.

    Discovery walks every historical session each poll and almost all of them are frozen;
    a directory's own mtime only moves when an entry is added or removed, so the listing is
    reused until it does. The file behind the listing is still stat'ed by the caller, because
    appends change its mtime and size and those are what the age gate and the tailer read.
    """
    try:
        stamp = os.stat(segments_dir).st_mtime_ns
    except OSError:
        return []
    hit = st.segments.get(segments_dir)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        with os.scandir(segments_dir) as it:
            names = sorted(e.name for e in it if e.name.endswith(".jsonl"))
    except OSError:
        return []
    if len(st.segments) > 400:
        st.segments.clear()
    st.segments[segments_dir] = (stamp, names)
    return names


def _newest_segment(segments_dir: str, st: _State) -> tuple[str, float, float, str] | None:
    names = _segment_names(segments_dir, st)
    if not names:
        return None
    # Run ids begin with a local timestamp, so lexical order is chronological.
    path = os.path.join(segments_dir, names[-1])
    try:
        info = os.stat(path)
    except OSError:
        return None
    return path, info.st_mtime, info.st_size, names[-1][:-6]


def _small(path: str) -> bool:
    try:
        return os.path.getsize(path) <= int(config.settings["full_read_kb"]) * 1024
    except OSError:
        return False
