"""Tunables (config.json) plus window geometry persistence."""

from __future__ import annotations

import json
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
GEOMETRY_PATH = os.path.join(APP_DIR, "geometry.json")

DEFAULTS: dict = {
    # Extra qoder homes to scan first; empty means auto-detect ~/.qoder-cn and ~/.qoder
    "qoder_homes": [],
    # Extra Copilot homes to scan first; empty means auto-detect ~/.copilot
    "copilot_homes": [],
    # Extra OpenCode data homes to scan first; empty means auto-detect ~/.local/share/opencode
    "opencode_homes": [],
    "poll_interval_ms": 1500,
    "tail_kb": 320,
    # First look at a log this size or smaller is read from the top so nothing at the
    # start of the session (model, transcript path, first prompt) is missed.
    "full_read_kb": 8192,
    # A session is dropped from the board after this much silence, even if its PID lives on
    "max_idle_minutes": 360,
    # A session with no execution claim stays "live" this long after its last activity,
    # so an idle-but-open session (claim cleared at turn end) keeps its colour.
    "live_minutes": 30,
    # Keep showing runs whose process already exited, for this long
    "include_recent_dead": True,
    "recent_dead_minutes": 10,
    "alpha_idle": 0.38,
    "alpha_hover": 1.0,
    "idle_presets": [0.22, 0.38, 0.55, 0.75],
    "max_cards": 5,
    # Sized to the widest row the board ever draws (the status line); rows clip to their
    # own pixel box, so anything wider is empty panel.
    "width": 360,
    "height": 340,
    "min_width": 300,
    "min_height": 170,
    "margin_right": 24,
    "margin_top": 48,
    "topmost": True,
    # Clicking a card raises the terminal / editor window that hosts that session
    "click_to_focus": True,
}

_GEOMETRY_KEYS = ("x", "y", "width", "height")

settings: dict = dict(DEFAULTS)


def load() -> dict:
    data = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    settings.clear()
    settings.update(DEFAULTS)
    settings.update({k: v for k, v in data.items() if k in DEFAULTS})
    return settings


def save() -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load_geometry() -> dict | None:
    try:
        with open(GEOMETRY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if all(k in data for k in _GEOMETRY_KEYS):
        return {k: int(data[k]) for k in _GEOMETRY_KEYS}
    return None


def save_geometry(geom: dict) -> None:
    try:
        with open(GEOMETRY_PATH, "w", encoding="utf-8") as fh:
            json.dump({k: int(geom[k]) for k in _GEOMETRY_KEYS}, fh, indent=2)
    except OSError:
        pass
