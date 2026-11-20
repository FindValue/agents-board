"""Directory listing that never raises: an agent home can vanish mid-poll."""

from __future__ import annotations

import os

import config


def dirs(path: str) -> list[str]:
    try:
        with os.scandir(path) as it:
            return sorted(e.name for e in it if e.is_dir())
    except OSError:
        return []


def homes(config_key: str, env_vars: tuple[str, ...], defaults: tuple[str, ...], marker: str) -> list[str]:
    """Candidate agent homes: configured ones first, then $ENV, then the well-known ones.

    Every CLI stores its logs under a home of its own and the search order is the same,
    so only the names and the marker that proves "this home is real" differ per adapter.
    """
    candidates = list(config.settings.get(config_key) or [])
    for name in env_vars:
        env = os.environ.get(name)
        if env:
            candidates.append(env)
    root = os.path.expanduser("~")
    candidates.extend(os.path.join(root, name) for name in defaults)

    found, seen = [], set()
    for path in candidates:
        full = os.path.abspath(os.path.expanduser(path))
        if full in seen:
            continue
        seen.add(full)
        if os.path.exists(os.path.join(full, marker)):
            found.append(full)
    return found


def project_name(cwd: str, fallback: str = "") -> str:
    """Bare directory name for the card title, since full paths never fit."""
    if cwd:
        return os.path.basename(cwd.rstrip("\\/")) or cwd
    return fallback
