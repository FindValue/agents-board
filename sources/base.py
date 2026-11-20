"""Adapter contract.

Adding another agent CLI is one file plus one line in SOURCES below: implement
scan() so that it returns AgentSession objects for that CLI's own log layout,
and keep all parsing inside the subclass.
"""

from __future__ import annotations

from core.state import AgentSession


class AgentSource:
    id = "base"
    label = "Base"

    def scan(self, cache: dict) -> list[AgentSession]:
        """Return the sessions worth showing right now.

        `cache` is a dict owned by this source across polls: keep file tailers,
        parsed state and PID results in it so repeated polls stay cheap.
        """
        raise NotImplementedError
