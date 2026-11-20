"""Incremental JSONL reader: each poll only pays for newly appended bytes."""

from __future__ import annotations

import json
import os


class JsonlTailer:
    def __init__(self, path: str) -> None:
        self.path = path
        self._pos = 0
        self._carry = b""
        self._skip_partial = False
        self._ever_read = False

    @property
    def at_start(self) -> bool:
        """True until the first successful read; the caller picks the window."""
        return self._pos == 0 and not self._ever_read

    def read(self, tail_bytes: int | None = None) -> list[dict]:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self._pos:  # truncated or replaced
            self.reset()
        if self._pos == 0 and tail_bytes and size > tail_bytes:
            self._pos = size - tail_bytes
            self._skip_partial = True
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self._pos)
                chunk = fh.read()
        except OSError:
            return []
        if not chunk:
            return []
        self._pos += len(chunk)
        self._ever_read = True

        parts = (self._carry + chunk).split(b"\n")
        self._carry = parts.pop()
        if parts and self._skip_partial:
            parts = parts[1:]
            self._skip_partial = False

        records = []
        for raw in parts:
            raw = raw.strip()
            if not raw:
                continue
            try:
                value = json.loads(raw.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    def reset(self) -> None:
        self._pos = 0
        self._carry = b""
        self._skip_partial = False
        self._ever_read = False
