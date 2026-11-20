"""UI strings, one JSON catalog per language under core/locales/.

`init()` runs after config.load(), `t(key, **kw)` formats the active catalog,
and `configure()` persists a menu choice and re-applies it immediately.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys

import config

_LOCALES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
# Languages offered in the menu; "auto" resolves to the system display language.
CHOICES = ("auto", "zh-CN", "en")
_LABELS = {"zh-CN": "简体中文", "en": "English"}

_catalogs: dict[str, dict[str, str]] = {}
_lang = "en"


def _load(code: str) -> dict[str, str]:
    if code not in _catalogs:
        path = os.path.join(_LOCALES, f"{code}.json")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            _catalogs[code] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _catalogs[code] = {}
    return _catalogs[code]


def detect() -> str:
    """Chinese UI language maps to zh-CN; anything else, including unknown, is English."""
    if sys.platform == "win32":
        try:
            lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            if lid in (0x0804, 0x0404, 0x0C04):
                return "zh-CN"
        except (AttributeError, OSError):
            pass
    return "en"


def _apply(choice: str) -> None:
    global _lang
    resolved = detect() if choice == "auto" else choice
    # A configured code with no catalog falls back to English.
    _lang = resolved if os.path.exists(os.path.join(_LOCALES, f"{resolved}.json")) else "en"


def init() -> None:
    _apply(config.settings.get("language", "auto"))


def configured() -> str:
    return config.settings.get("language", "auto")


def configure(code: str) -> None:
    config.settings["language"] = code
    config.save()
    _apply(code)


def lang() -> str:
    return _lang


def choice_label(code: str) -> str:
    return t("lang_auto") if code == "auto" else _LABELS[code]


def t(key: str, **kw) -> str:
    text = _load(_lang).get(key, key)
    return text.format(**kw) if kw else text
