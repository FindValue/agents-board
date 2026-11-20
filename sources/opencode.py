"""OpenCode adapter (the opencode CLI's own SQLite store).

Layout under the OpenCode data home (~/.local/share/opencode by default):

    opencode.db (+ -wal)    session_v2: one row per session, session_message: one per record

The TUI keeps the newest message row open and rewrites its `data` in place while a turn
streams, so the incremental cursor is `time_updated` rather than `seq`. Nothing in the
database records a pid, so a card is only clickable when the running TUI can be matched
by the directory it was launched from — and that same mapping is the only way to tell an
open window from session history: each window displays one session (`_claim`), so any
session no window claims is history that only survives the just-exited grace window.
Sub-agent sessions are folded into their parent's `subtask` rows, hence `parent_id IS NULL`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field

import config
from core.fs import homes, project_name
from core.pid import console_of, cwd_of, processes
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
)
from .base import AgentSource

_HOME_DIRS = (".local/share/opencode",)
_HOME_DB = "opencode.db"
# Newest sessions by activity, capped so a large store cannot stall a poll.
_SESSIONS = """
SELECT id, directory, slug, model, cost,
       tokens_input, tokens_output, tokens_cache_read,
       time_created, time_updated, time_compacting, time_suspended
FROM session_v2 WHERE parent_id IS NULL
ORDER BY time_updated DESC LIMIT 50
"""
_RECORDS = "SELECT seq, type, data, time_updated FROM session_message WHERE session_id=? AND time_updated>? ORDER BY seq"
_TAIL = "SELECT seq, type, data, time_updated FROM session_message WHERE session_id=? ORDER BY seq DESC LIMIT ?"
_TURN_COUNT = "SELECT COUNT(*) FROM session_message WHERE session_id=? AND type='user'"
# A subagent's work lives in its own session (parent_id set); the parent's message
# stream can end at idle while a child still holds an execution claim. Only the claim
# counts as "running": unlike the parent, a subagent has no "open but waiting for
# input" gap, so a fresh time_updated from a finished child would pin the card on
# 子代理运行中 long after it is done.
_BUSY_CHILDREN = (
    "SELECT parent_id, COUNT(*) AS n FROM session_v2 "
    "WHERE parent_id IS NOT NULL AND time_suspended IS NOT NULL "
    "GROUP BY parent_id"
)

# Cold start reads the tail only; the newest rows are what the state machine needs, and
# the counters accumulate from there — the same trade the jsonl adapters make at tail_kb.
_TAIL_ROWS = 60
_KEEP_RECORDS = 40
# A pending tool part is normal for a fraction of a second; a stuck one is the TUI
# waiting on a permission prompt, which is never written to the database.
_AWAIT_SECONDS = 6.0
# One snapshot of every process plus a console/PEB read per candidate; the TUI neither
# moves nor renames itself between polls.
_HOST_TTL = 5.0


def _dir_key(path) -> str:
    if not isinstance(path, str):
        return ""
    return path.replace("\\", "/").rstrip("/").lower()


def _ms(value) -> float:
    return value / 1000 if isinstance(value, (int, float)) else 0.0


def _intact(path: str) -> bool:
    """False once the store vanished or was replaced by something smaller."""
    try:
        return os.path.getsize(path) >= 4096
    except OSError:
        return False


@dataclass
class _Part:
    id: str
    name: str
    status: str
    label: str
    todos: list = field(default_factory=list)


@dataclass
class _Record:
    seq: int
    kind: str
    ts: float
    text: str = ""
    finish: str = ""
    opened: float = 0.0
    closed: float = 0.0
    parts: list[_Part] = field(default_factory=list)


def _parts(content, ts: float) -> list[_Part]:
    out = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict) or part.get("type") != "tool":
            continue
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        arguments = state.get("input") if isinstance(state.get("input"), dict) else {}
        todos = arguments.get("todos")
        out.append(
            _Part(
                id=str(part.get("id") or f"{ts}:{len(out)}"),
                name=str(part.get("name") or "tool"),
                status=str(state.get("status") or ""),
                label=label_for(arguments),
                todos=todos if isinstance(todos, list) else [],
            )
        )
    return out


def _parse(seq: int, kind: str, updated: int, data) -> _Record | None:
    try:
        blob = json.loads(data)
    except (TypeError, ValueError):
        return None
    if not isinstance(blob, dict):
        return None
    record = _Record(seq=seq, kind=kind, ts=_ms(updated))
    if kind == "user":
        record.text = first_line(blob.get("text"))
    elif kind == "idle":
        record.text = str(blob.get("outcome") or "")
    elif kind == "assistant":
        times = blob.get("time") if isinstance(blob.get("time"), dict) else {}
        record.opened = _ms(times.get("created")) or record.ts
        record.closed = _ms(times.get("completed"))
        record.finish = str(blob.get("finish") or "")
        record.parts = _parts(blob.get("content"), record.ts)
    return record


@dataclass
class _Fold:
    """The records currently held for one session, plus the counts that only go up."""

    records: dict[int, _Record] = field(default_factory=dict)
    tool_calls: int = 0
    errors: int = 0

    def feed(self, record: _Record) -> None:
        previous = self.records.get(record.seq)
        known = {part.id: part.status for part in previous.parts} if previous else {}
        for part in record.parts:
            # The row is rewritten on every status change, so a part is only new when
            # its id was not in this row last time, and only fails on the transition.
            if part.id not in known:
                self.tool_calls += 1
            if part.status == "error" and known.get(part.id) != "error":
                self.errors += 1
        self.records[record.seq] = record
        if len(self.records) > _KEEP_RECORDS:
            for seq in sorted(self.records)[: -_KEEP_RECORDS]:
                del self.records[seq]

    def ordered(self) -> list[_Record]:
        return [self.records[seq] for seq in sorted(self.records)]

    def prompt(self) -> str:
        for record in reversed(self.ordered()):
            if record.kind == "user" and record.text:
                return record.text
        return ""

    def tasks(self) -> tuple[int, int, str]:
        for record in reversed(self.ordered()):
            for part in record.parts:
                if part.name not in ("todowrite", "todo") or not part.todos:
                    continue
                items = []
                for item in part.todos:
                    if isinstance(item, dict):
                        content = str(item.get("content") or item.get("subject") or "").strip()
                        if content:
                            items.append((content, str(item.get("status") or "")))
                if not items:
                    continue
                done = sum(1 for _, status in items if status in ("completed", "done"))
                current = next((text for text, status in items if status not in ("completed", "done")), "")
                return done, len(items), current
        return 0, 0, ""

    def activity(self) -> list[str]:
        out = []
        for record in self.ordered():
            for part in record.parts:
                if part.status in ("completed", "error"):
                    out.append(f"{hms(record.ts)} {part.name} {'失败' if part.status == 'error' else 'ok'}")
        return out[-3:]

    def derive(self, now: float, compacting: float) -> tuple[str, str]:
        records = self.ordered()
        if not records:
            return "starting", "初始化中"
        newest = records[-1]
        opened = max((r.seq for r in records if r.kind == "user"), default=0)
        in_turn = [part for r in records if r.seq > opened for part in r.parts]
        stuck = in_turn[-1] if in_turn else None

        if newest.kind == "idle":
            outcome = newest.text or "succeeded"
            if outcome not in ("succeeded", ""):
                return "error", f"上一轮 {clip(outcome, 40)}"
            waited = fmt_age(now - newest.ts)
            last = max((r for r in records if r.kind == "assistant"), key=lambda r: r.seq, default=None)
            # `completed=None` means the assistant row was never closed — the model may
            # still be streaming (the idle row marks the *previous* turn's end, and the
            # new user prompt hasn't landed in the database yet).
            if last and not last.closed:
                return "model", f"生成回复中 · {fmt_age(now - newest.ts)}"
            if last and last.closed:
                return "idle", f"上轮 {fmt_duration(max(0.0, last.closed - last.opened))} · {waited}"
            return "idle", f"等待输入 · {waited}"
        if compacting and compacting >= newest.ts:
            return "compact", "压缩中"
        if newest.kind == "compaction":
            return "compact", "压缩中"

        if stuck and stuck.status == "error":
            return "error", f"{stuck.name} 失败"
        # The question tool sits pending→running until the user answers, so an
        # unfinished question part IS the ask — not a permission wait (those never
        # reach the database) and not "执行工具".
        if stuck and stuck.name == "question" and stuck.status in ("pending", "running"):
            return "asking", f"{clip(stuck.label, 40)} ({fmt_age(now - newest.ts)})"
        if stuck and stuck.status == "pending" and now - newest.ts > _AWAIT_SECONDS:
            waited = fmt_age(now - newest.ts)
            return "awaiting", f"{stuck.name}: {clip(stuck.label, 40)} 等待授权 ({waited})"
        if newest.kind in ("subtask", "branch"):
            count = sum(1 for r in records if r.seq > opened and r.kind in ("subtask", "branch"))
            return "subagent", f"{count} 个子任务"
        if stuck and stuck.status in ("pending", "running"):
            return "tool", f"{stuck.name}: {clip(stuck.label, 40)} ({fmt_age(now - newest.ts)})"
        return "model", f"生成回复中 · {fmt_age(now - newest.ts)}"


@dataclass
class _Hosts:
    stamp: float = 0.0
    by_dir: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class _State:
    conn: sqlite3.Connection | None = None
    path: str = ""
    watermark: dict[str, int] = field(default_factory=dict)
    folds: dict[str, _Fold] = field(default_factory=dict)
    hosts: _Hosts = field(default_factory=_Hosts)


class OpencodeSource(AgentSource):
    id = "opencode"
    label = "OpenCode"

    def scan(self, cache: dict) -> list[AgentSession]:
        st = cache.get("state") or _State()
        cache["state"] = st
        now = time.time()

        conn = self._connect(st)
        if conn is None:
            return []
        rows = list(conn.execute(_SESSIONS))
        hosts = self._hosts(st, now)
        windows = self._claim(rows, hosts, now)
        busy_children = {r["parent_id"]: r["n"] for r in conn.execute(_BUSY_CHILDREN)}
        sessions = []
        for row in rows:
            session = self._snapshot(
                conn, row, now, st, windows.get(row["id"], 0), busy_children
            )
            if session is not None:
                sessions.append(session)

        keys = {s.key for s in sessions}
        st.watermark = {k: v for k, v in st.watermark.items() if k in keys}
        st.folds = {k: v for k, v in st.folds.items() if k in keys}
        return sessions

    def _claim(self, rows: list[sqlite3.Row], hosts: _Hosts, now: float) -> dict[str, int]:
        """Match each running TUI window to at most one session it may be displaying.

        A window is a *best guess* for click-to-focus only: location switches change
        `session_v2.directory` without moving the TUI's PEB cwd, so several live sessions
        can share the same window. Every one of them stays clickable to it — otherwise a
        card with no pid silently ignores the click. Liveness never depends on winning a
        window, and a dead session must never consume one from a live one.
        """
        claimed: dict[str, int] = {}
        all_pids = [pid for pids in hosts.by_dir.values() for pid in pids]
        free = list(all_pids)
        live = []
        for row in rows:
            age = now - _ms(row["time_updated"])
            alive = row["time_suspended"] is not None or age < config.settings["live_minutes"] * 60
            if not dropped(age, alive):
                live.append((row, _dir_key(row["directory"])))
        for row, directory in live:
            if not free:
                break
            exact = [pid for pid in hosts.by_dir.get(directory, []) if pid in free]
            if exact:
                pid = exact[0]
            else:
                deeper = [
                    (cwd, pid)
                    for cwd, pids in hosts.by_dir.items()
                    if directory.startswith(cwd + "/")
                    for pid in pids
                    if pid in free
                ]
                if not deeper:
                    continue
                pid = max(deeper, key=lambda item: len(item[0]))[1]
            free.remove(pid)
            claimed[row["id"]] = pid
        # A location switch moves session_v2.directory but not the TUI's PEB cwd, so a
        # live session can have no cwd-matched host; its window is the console stub left
        # behind by whichever directory the TUI was launched from. Several live sessions
        # can also be tabs of one TUI window. Share *all* hosts with the unclaimed
        # sessions, not just the leftovers: with one pid and several live sessions the
        # first match drains `free`, leaving the rest pid=0 and their clicks dead.
        if all_pids:
            pending = [row for row, _ in live if row["id"] not in claimed]
            for i, row in enumerate(pending):
                claimed[row["id"]] = all_pids[i % len(all_pids)]
        return claimed

    # -- database ------------------------------------------------------------

    def _connect(self, st: _State) -> sqlite3.Connection | None:
        """One read-only connection, reopened when the store moves or shrinks.

        immutable is deliberately not set: the TUI's committed rows live in the -wal
        until a checkpoint, and an immutable read of that file sees an empty database.
        """
        found = homes("opencode_homes", ("OPENCODE_DATA",), _HOME_DIRS, _HOME_DB)
        path = os.path.join(found[0], _HOME_DB) if found else ""
        if st.conn is not None and (not path or st.path != path or not _intact(path)):
            st.conn.close()
            st.conn = None
            st.path = ""
            st.watermark.clear()
            st.folds.clear()
        if st.conn is None and path:
            uri = f"file:{os.path.abspath(path).replace(os.sep, '/')}?mode=ro"
            try:
                conn = sqlite3.connect(uri, uri=True, timeout=1.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=1")
            except (sqlite3.Error, OSError):
                return None
            st.conn = conn
            st.path = path
        return st.conn

    def _records(self, conn: sqlite3.Connection, key: str, st: _State) -> list[_Record]:
        cursor = st.watermark.get(key, 0)
        if cursor:
            rows = conn.execute(_RECORDS, (key, cursor)).fetchall()
        else:
            rows = list(conn.execute(_TAIL, (key, _TAIL_ROWS)))
            rows.reverse()
        parsed = []
        newest = cursor
        for row in rows:
            newest = max(newest, row["time_updated"])
            record = _parse(row["seq"], row["type"], row["time_updated"], row["data"])
            if record is not None:
                parsed.append(record)
        st.watermark[key] = newest
        return parsed

    # -- discovery -----------------------------------------------------------

    def _hosts(self, st: _State, now: float) -> _Hosts:
        """The TUI processes that own a console, indexed by where they were started."""
        hosts = st.hosts
        if now - hosts.stamp < _HOST_TTL:
            return hosts
        hosts = _Hosts(stamp=now)
        for pid, (name, _) in processes().items():
            if not name.startswith("opencode"):
                continue
            console, _title = console_of(pid)
            # `opencode serve` owns no console, so it can never be the window to raise.
            if not console:
                continue
            directory = _dir_key(cwd_of(pid))
            if directory:
                hosts.by_dir.setdefault(directory, []).append(pid)
        st.hosts = hosts
        return hosts

    def _snapshot(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        now: float,
        st: _State,
        pid: int,
        busy_children: dict[str, int],
    ) -> AgentSession | None:
        key = row["id"]
        updated = _ms(row["time_updated"])
        age = now - updated
        # A session is "open" if it has an execution claim (`time_suspended`) OR it
        # wrote anything recently. The claim is cleared at turn end, so an idle TUI
        # session would look closed by claim alone; the recent-activity window covers
        # that gap. A closed session has neither, and ages out via `dropped()`.
        # Window match (`pid`) is only click-to-focus, never liveness.
        claimed = row["time_suspended"] is not None
        alive = claimed or age < config.settings["live_minutes"] * 60
        if dropped(age, alive):
            return None

        fold = st.folds.get(key)
        if fold is None:
            fold = _Fold()
            st.folds[key] = fold
        for record in self._records(conn, key, st):
            fold.feed(record)

        path = row["directory"] or ""
        state, detail = fold.derive(now, _ms(row["time_compacting"]))
        if not alive:
            state, detail = dead_detail(age)
        elif key in busy_children and state in ("idle", "starting"):
            # The parent's own stream ended at idle, but its subagents are still
            # working — that is "子代理运行中", not "空闲待输入".
            n = busy_children[key]
            state, detail = "subagent", f"{n} 个子代理运行中"

        turn_count = conn.execute(_TURN_COUNT, (key,)).fetchone()[0]
        tasks_done, tasks_total, current_task = fold.tasks()
        return AgentSession(
            source=self.id,
            key=key,
            project=project_name(path, row["slug"] or key[:8]),
            cwd=path,
            pid=pid,
            alive=alive,
            model=self._model(row["model"]),
            prompt=clip(fold.prompt() or row["slug"] or "", 78),
            state=state,
            detail=detail,
            started_at=_ms(row["time_created"]) or updated,
            last_event_at=updated,
            turns=turn_count,
            tool_calls=fold.tool_calls,
            errors=fold.errors,
            tasks_done=tasks_done,
            tasks_total=tasks_total,
            current_task=current_task,
            usage=self._usage(row),
            activity=fold.activity(),
        )

    @staticmethod
    def _model(value) -> str:
        try:
            blob = json.loads(value)
        except (TypeError, ValueError):
            return ""
        return str(blob.get("id") or "") if isinstance(blob, dict) else ""

    @staticmethod
    def _usage(row: sqlite3.Row) -> str:
        """session_v2 carries session totals, so no windowed sum is needed."""
        inp, outp, cache = row["tokens_input"], row["tokens_output"], row["tokens_cache_read"]
        if not (inp or outp):
            return ""
        cost = row["cost"] or 0.0
        return " · ".join(
            p
            for p in (
                f"输入 {fmt_tokens(inp or 0)}",
                f"输出 {fmt_tokens(outp or 0)}",
                f"缓存 {fmt_tokens(cache)}" if cache else "",
                f"成本 ${cost:.2f}" if cost else "",
            )
            if p
        )
