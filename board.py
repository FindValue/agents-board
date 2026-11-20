"""Always-on-top transparent HUD showing live agent-CLI progress.

    python board.py              # floating board
    python board.py --once       # print the board once, no window
    python board.py --once --repeat

Borderless window: drag the header to move it, pull the bottom-right grip to
resize, right-click anywhere for the menu, double-click the header to freeze the
opacity, click a card to raise the terminal or editor that hosts that session.
Hover fades the whole window to opaque.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import tkinter as tk
import tkinter.font as tkfont

import config
import sources
from core import i18n, winui
from core.state import AgentSession, clip, fmt_age, fmt_duration, state_color, state_label

PANEL = "#0e1216"
HEADER = "#141a21"
LINE = "#232d37"
TEXT = "#dbe3ea"
DIM = "#93a0ad"
MUTED = "#78848f"
WARN = "#ff9f43"

ALPHA_STEP = 0.06
ANIM_MS = 33
BLINK_MS = 620
FOOTER_HOLD = 6.0
ROW_H = 18
CARD_H = 104
HEADER_H = 24
FOOTER_H = 18
GRIP = 20
DOT_BOX = 16
DOT_R = 4.5
DOT_RING = 7.0
EDGE = 8

# label -> (x, y, width as a fraction of the card or None for "up to the right edge",
# anchor) in design pixels
CARD_ROWS = {
    "title": (26, 6, 0.52, "w"),
    "meta": (-EDGE, 6, 0.44, "ne"),
    "prompt": (26, 27, None, "w"),
    "status": (EDGE, 47, None, "w"),
    "counts": (EDGE, 67, None, "w"),
    "task": (EDGE, 84, None, "w"),
}
DOT_ROW = (EDGE, 9, "w")


def row_px(card_w: int, name: str) -> int:
    """Device pixels a row's text may occupy, so clipping can measure against it."""
    x, _y, relw, _anchor = CARD_ROWS[name]
    if relw is not None:
        return max(1, int(card_w * relw))
    return max(1, card_w - winui.px(abs(x)) - winui.px(EDGE) - winui.px(2))


def mix(foreground: str, background: str, weight: float) -> str:
    """Blend two #rrggbb colours; used to fake anti-aliasing the canvas cannot do."""
    top = [int(foreground[i:i + 2], 16) for i in (1, 3, 5)]
    back = [int(background[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(
        int(round(back[i] + (top[i] - back[i]) * weight)) for i in range(3)
    )


class Card(tk.Frame):
    """One session row. Widgets are built once and rewritten in place."""

    def __init__(self, master, fonts: dict, activate) -> None:
        super().__init__(master, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        self.fonts = fonts
        self.activate = activate
        self.target: AgentSession | None = None
        self.signature = ""
        self.box = 0
        self.state = "idle"
        self.needs_attention = False

        self.dot = tk.Canvas(self, bg=PANEL, highlightthickness=0)
        self.title = self._label(PANEL, "bold", TEXT)
        self.meta = self._label(PANEL, "small", DIM, "e")
        self.prompt = self._label(PANEL, "body", DIM)
        self.status = self._label(PANEL, "bold", TEXT)
        self.counts = self._label(PANEL, "mono", MUTED)
        self.task = self._label(PANEL, "small", DIM)

        for name in CARD_ROWS:
            self._place(name)
        self._dot()
        for widget in self.winfo_children():
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>", self._clicked)
        self.bind("<Button-1>", self._clicked)

    def _clicked(self, event) -> str:
        if self.target is not None:
            self.activate(self.target)
        return "break"

    def _place(self, name: str) -> None:
        x, y, relw, anchor = CARD_ROWS[name]
        widget = getattr(self, name)
        box = {"relwidth": relw if relw is not None else 1.0}
        if relw is None:
            box["width"] = -winui.px(abs(x) + EDGE + 2)
        widget.place(x=winui.px(x), y=winui.px(y), height=winui.px(ROW_H), anchor=anchor, **box)

    def _dot(self) -> None:
        side = winui.px(DOT_BOX)
        self.dot.configure(width=side, height=side)
        self.dot.place(x=winui.px(DOT_ROW[0]), y=winui.px(DOT_ROW[1]), anchor=DOT_ROW[2])

    def _label(self, bg: str, font_key: str, fg: str, anchor: str = "w") -> tk.Label:
        return tk.Label(self, bg=bg, fg=fg, font=self.fonts[font_key], anchor=anchor)

    def _fit(self, font_key: str, text: str, px: int) -> str:
        return clip(text, px, self.fonts[font_key].measure)

    def show(self, session: AgentSession, card_w: int, tag: str = "") -> None:
        head = session.project or session.key
        if tag:
            head = f"[{tag}] {head}"
        if not session.alive:
            head += i18n.t("card_exited")
        self.title.configure(
            text=self._fit("bold", head, row_px(card_w, "title")), fg=TEXT if session.alive else MUTED
        )
        trail = " ".join(p for p in (session.branch, session.model) if p)
        span = fmt_duration(session.uptime()) if session.alive else fmt_age(session.idle_age())
        self.meta.configure(
            text=self._fit("small", f"{trail} {span}".strip(), row_px(card_w, "meta")),
            fg=DIM if session.alive else MUTED,
        )
        self.prompt.configure(text=self._fit("body", session.prompt or i18n.t("no_prompt"), row_px(card_w, "prompt")))

        label = state_label(session.state)
        self.status.configure(
            text=self._fit("bold", f"{label} · {session.detail}" if session.detail else label, row_px(card_w, "status")),
            fg=state_color(session.state) if session.alive else MUTED,
        )
        parts = [
            i18n.t("count_turn", n=session.turns),
            i18n.t("count_tool", n=session.tool_calls),
        ]
        if session.errors:
            parts.append(i18n.t("count_error", n=session.errors))
        if session.usage:
            parts.append(session.usage)
        self.counts.configure(text=self._fit("mono", " · ".join(parts), row_px(card_w, "counts")))

        if session.tasks_total:
            bar = i18n.t("card_task", done=session.tasks_done, total=session.tasks_total)
            text = f"{bar} · {session.current_task}" if session.current_task else bar
            fg = TEXT if session.tasks_done == session.tasks_total else DIM
        elif session.activity:
            text, fg = session.activity[-1], MUTED
        else:
            text, fg = "", MUTED
        if text:
            self.task.configure(text=self._fit("small", text, row_px(card_w, "task")), fg=fg)
            self._place("task")
        else:
            self.task.configure(text="")
            self.task.place_forget()

        self.state = session.state
        self.needs_attention = session.attention
        self.paint_dot(state_color(session.state), dim=not session.alive)

    def paint_dot(self, color: str, dim: bool = False) -> None:
        self.dot.delete("all")
        shade = MUTED if dim else color
        if self.needs_attention and not dim:
            self._disc(DOT_RING + 1, mix(shade, PANEL, 0.3))
            self._disc(DOT_RING, mix(shade, PANEL, 0.8))
            self._disc(DOT_RING - 2, PANEL)
        core = DOT_R * (0.55 if dim else 1.0)
        self._disc(core + 1, mix(shade, PANEL, 0.3))
        self._disc(core + 0.5, mix(shade, PANEL, 0.65))
        self._disc(core, shade)

    def _disc(self, radius: float, fill: str) -> None:
        centre = winui.px(DOT_BOX) / 2.0
        edge = winui.px(radius)
        self.dot.create_oval(centre - edge, centre - edge, centre + edge, centre + edge, fill=fill, outline="")


class Board:
    def __init__(self) -> None:
        self.settings = config.settings
        winui.become_aware()
        self.root = tk.Tk()
        winui.scale(self.root)
        self.root.overrideredirect(True)
        self.root.configure(bg=PANEL)
        self.root.attributes("-topmost", bool(self.settings["topmost"]))
        self.root.attributes("-alpha", float(self.settings["alpha_idle"]))

        self.fonts = self._fonts()
        self.cards: list[Card] = []
        self.sessions: list[AgentSession] = []
        self._alpha = float(self.settings["alpha_idle"])
        self._idle_alpha = float(self.settings["alpha_idle"])
        self._hover = False
        self._locked = False
        self._blink = True
        self._scan_ms = 0.0
        self._auto_height = True
        self._held: tuple[str, float] = ("", 0.0)
        self._drag: tuple | None = None

        self._build()
        self._place_initial()
        self.root.update_idletasks()
        winui.round_corners(self.root)
        self._bind()
        self._build_menu()
        self.root.after(80, self._poll)
        self.root.after(ANIM_MS, self._animate)

    # -- construction --------------------------------------------------------

    def _fonts(self) -> dict:
        family = "Microsoft YaHei UI"
        size = tkfont.nametofont("TkDefaultFont").cget("size")
        return {
            "body": tkfont.Font(family=family, size=size),
            "bold": tkfont.Font(family=family, size=size, weight="bold"),
            "small": tkfont.Font(family=family, size=max(7, size - 1)),
            "mono": tkfont.Font(family="Consolas", size=max(7, size - 1)),
        }

    def _build(self) -> None:
        self.panel = tk.Frame(self.root, bg=PANEL)
        self.panel.pack(fill="both", expand=True)
        self.header = tk.Frame(self.panel, bg=HEADER)
        self.header_title = tk.Label(
            self.header, text=i18n.t("app_title"), bg=HEADER, fg=DIM, font=self.fonts["bold"], anchor="w"
        )
        self.header_hint = tk.Label(
            self.header, text=i18n.t("header_hint"), bg=HEADER, fg=MUTED, font=self.fonts["small"]
        )
        self.body = tk.Frame(self.panel, bg=PANEL)
        self.footer = tk.Label(self.panel, text="", bg=PANEL, fg=MUTED, font=self.fonts["small"], anchor="w")
        self.grip = tk.Canvas(
            self.panel, width=winui.px(GRIP), height=winui.px(GRIP), bg=PANEL, highlightthickness=0,
            cursor="size_nw_se",
        )
        for row in range(3):
            for column in range(row + 1):
                x = winui.px(GRIP - 5 - (row - column) * 5)
                y = winui.px(GRIP - 13 + row * 5)
                self.grip.create_rectangle(x, y, x + winui.px(2), y + winui.px(2), fill=MUTED, outline="")

        self.header_title.pack(side="left", fill="y", padx=(winui.px(8), 0))
        self.header_hint.pack(side="right", fill="y", padx=(0, winui.px(8)))
        self.header.place(x=0, y=0, relwidth=1.0, height=winui.px(HEADER_H))
        self.body.place(
            x=winui.px(2), y=winui.px(HEADER_H + 3), relwidth=1.0, width=-winui.px(6),
            relheight=1.0, height=-winui.px(HEADER_H + FOOTER_H + 8),
        )
        self.footer.place(
            x=winui.px(8), rely=1.0, anchor="sw", relwidth=1.0, width=-winui.px(30), height=winui.px(FOOTER_H)
        )
        self.grip.place(relx=1.0, rely=1.0, anchor="se", x=-winui.px(2), y=-winui.px(2))

    def _place_initial(self) -> None:
        """Everything here is design pixels; Tk is handed device pixels."""
        geom = config.load_geometry() or {}
        width = int(geom.get("width") or self.settings["width"])
        height = int(geom.get("height") or self.settings["height"])
        screen_w = winui.device(self.root.winfo_screenwidth())
        screen_h = winui.device(self.root.winfo_screenheight())
        x = int(geom.get("x", screen_w - width - int(self.settings["margin_right"])))
        y = int(geom.get("y", int(self.settings["margin_top"])))
        self._resize(
            winui.px(width),
            winui.px(height),
            winui.px(max(0, min(x, screen_w - width))),
            winui.px(max(0, min(y, screen_h - 60))),
        )

    def _resize(self, width: int, height: int, x: int | None = None, y: int | None = None) -> None:
        left = self.root.winfo_x() if x is None else int(x)
        top = self.root.winfo_y() if y is None else int(y)
        self.root.geometry(f"{int(width)}x{int(height)}+{left}+{top}")

    def _bind(self) -> None:
        for widget in (self.header, self.header_title, self.header_hint):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_stop)
            widget.bind("<Double-Button-1>", self._toggle_lock)
        self.grip.bind("<ButtonPress-1>", self._resize_start)
        self.grip.bind("<B1-Motion>", self._resize_move)
        self.grip.bind("<ButtonRelease-1>", self._resize_stop)
        self.root.bind_all("<ButtonPress-3>", self._menu_popup)
        self.root.bind("<Destroy>", self._on_destroy)

    # -- options and menu ----------------------------------------------------

    def _build_menu(self) -> None:
        menu = tk.Menu(
            self.root, tearoff=0, bg=PANEL, fg=TEXT, activebackground=HEADER, activeforeground=TEXT,
            bd=0, activeborderwidth=0,
        )
        self._vars = {
            "pin": tk.BooleanVar(master=self.root, value=bool(self.settings["topmost"])),
            "dead": tk.BooleanVar(master=self.root, value=bool(self.settings["include_recent_dead"])),
            "auto": tk.BooleanVar(master=self.root, value=self._auto_height),
            "idle": tk.DoubleVar(master=self.root, value=self._idle_alpha),
            "jump": tk.BooleanVar(master=self.root, value=bool(self.settings["click_to_focus"])),
        }
        flags = (
            ("pin", "menu_pin"),
            ("auto", "menu_auto"),
            ("dead", "menu_dead"),
            ("jump", "menu_jump"),
        )
        # 勾选态在深色菜单上是一块看不清的选中条：开关状态写在 accelerator 列的圆点里。
        # Tk 把 cascade 箭头也当 accelerator 计宽（tkWinMenu.c GetMenuAccelGeometry），
        # 两者同列起笔，activeborderwidth=0 时 accelerator 顶到条目右缘，圆点与箭头对齐。
        self._flag_entries = []
        for key, label_key in flags:
            menu.add_checkbutton(
                label=i18n.t(label_key),
                variable=self._vars[key],
                indicatoron=False,
                accelerator=self._dot(key),
                command=lambda key=key: self._apply_flag(key),
            )
            self._flag_entries.append((menu.index("end"), key))
        opacity = tk.Menu(
            menu, tearoff=0, bg=PANEL, fg=TEXT, activebackground=HEADER, activeforeground=TEXT,
            activeborderwidth=0,
        )
        for preset in self.settings["idle_presets"]:
            opacity.add_radiobutton(
                label=f"{int(preset * 100)}%",
                value=float(preset),
                indicatoron=False,
                variable=self._vars["idle"],
                accelerator=self._preset_dot(preset),
                command=self._apply_idle,
            )
        menu.add_cascade(label=i18n.t("menu_opacity"), menu=opacity)
        self._opacity = opacity
        lang_menu = tk.Menu(
            menu, tearoff=0, bg=PANEL, fg=TEXT, activebackground=HEADER, activeforeground=TEXT,
            activeborderwidth=0,
        )
        self._lang_var = tk.StringVar(master=self.root, value=i18n.configured())
        for code in i18n.CHOICES:
            lang_menu.add_radiobutton(
                label=i18n.choice_label(code),
                value=code,
                variable=self._lang_var,
                indicatoron=False,
                accelerator=self._lang_dot(code),
                command=lambda code=code: self._apply_lang(code),
            )
        menu.add_cascade(label=i18n.t("menu_language"), menu=lang_menu)
        menu.add_separator()
        menu.add_command(label=i18n.t("menu_refresh"), command=self._poll)
        menu.add_command(label=i18n.t("menu_copy_cwd"), command=self._copy_cwd)
        menu.add_separator()
        menu.add_command(label=i18n.t("menu_restart"), command=self._restart)
        menu.add_command(label=i18n.t("menu_quit"), command=self.root.destroy)
        self.menu = menu

    def _menu_popup(self, event) -> None:
        self.menu.tk_popup(event.x_root, event.y_root)

    def _apply_pin(self) -> None:
        value = self._vars["pin"].get()
        self.settings["topmost"] = value
        self.root.attributes("-topmost", value)

    def _apply_auto(self) -> None:
        self._auto_height = self._vars["auto"].get()
        self._render()

    def _apply_dead(self) -> None:
        self.settings["include_recent_dead"] = self._vars["dead"].get()

    def _apply_idle(self) -> None:
        self._idle_alpha = float(self._vars["idle"].get())
        for i, preset in enumerate(self.settings["idle_presets"]):
            self._opacity.entryconfigure(i, accelerator=self._preset_dot(preset))

    def _preset_dot(self, preset: float) -> str:
        return "●" if abs(preset - self._idle_alpha) < 0.005 else "○"

    def _apply_jump(self) -> None:
        self.settings["click_to_focus"] = self._vars["jump"].get()

    def _apply_flag(self, key: str) -> None:
        getattr(self, f"_apply_{key}")()
        self._refresh_flags()

    def _refresh_flags(self) -> None:
        for idx, key in self._flag_entries:
            self.menu.entryconfigure(idx, accelerator=self._dot(key))

    def _dot(self, key: str) -> str:
        return "●" if self._vars[key].get() else "○"

    def _lang_dot(self, code: str) -> str:
        return "●" if i18n.configured() == code else "○"

    def _apply_lang(self, code: str) -> None:
        i18n.configure(code)
        self._build_menu()
        self.header_title.configure(text=i18n.t("app_title"))
        self.header_hint.configure(text=self._hint())
        self._poll()

    def _hint(self) -> str:
        return i18n.t("header_locked") if self._locked else i18n.t("header_hint")

    def _toggle_lock(self, event=None) -> str:
        self._locked = not self._locked
        self.header_hint.configure(
            text=self._hint(), fg=WARN if self._locked else MUTED
        )
        return "break"

    def _activate(self, session: AgentSession) -> None:
        if not self.settings["click_to_focus"] or session.pid <= 0:
            return
        name = session.project or session.key
        action = winui.focus_process(session.pid, session.key)
        if action == "raised":
            self._note(i18n.t("note_raised", name=name))
        elif action == "back":
            self._note(i18n.t("note_back"))
        elif action == "tab":
            self._note(i18n.t("note_tab", name=name))
        else:
            self.root.bell()
            self._note(i18n.t("note_no_window", name=name))

    def _note(self, text: str) -> None:
        """A click's answer has to outlive the next poll, or it is never readable."""
        self._held = (text, time.time())
        self._footer(text)

    def _copy_cwd(self) -> None:
        for session in self.sessions:
            if session.cwd:
                self.root.clipboard_clear()
                self.root.clipboard_append(session.cwd)
                self._note(i18n.t("note_copied", path=session.cwd))
                return
        self.root.bell()

    def _restart(self) -> None:
        """Respawn the board detached so it outlives this process, then tear down."""
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__)],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        self.root.destroy()

    # -- interaction ---------------------------------------------------------

    def _drag_start(self, event) -> None:
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event) -> None:
        if self._drag and len(self._drag) == 2:
            self._resize(
                self.root.winfo_width(), self.root.winfo_height(),
                event.x_root - self._drag[0], event.y_root - self._drag[1],
            )

    def _drag_stop(self, event) -> None:
        self._drag = None
        self._save_geometry()

    def _resize_start(self, event) -> None:
        self._vars["auto"].set(False)
        self._auto_height = False
        self._refresh_flags()
        self._drag = (event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height())

    def _resize_move(self, event) -> None:
        if not self._drag or len(self._drag) != 4:
            return
        origin_x, origin_y, start_w, start_h = self._drag
        width = max(winui.px(int(self.settings["min_width"])), start_w + event.x_root - origin_x)
        height = max(winui.px(int(self.settings["min_height"])), start_h + event.y_root - origin_y)
        self._resize(width, height)

    def _resize_stop(self, event) -> None:
        self._drag = None
        self._save_geometry()

    def _save_geometry(self) -> None:
        config.save_geometry(
            {
                "x": winui.device(self.root.winfo_x()),
                "y": winui.device(self.root.winfo_y()),
                "width": winui.device(self.root.winfo_width()),
                "height": winui.device(self.root.winfo_height()),
            }
        )

    def _on_destroy(self, event) -> None:
        if event.widget is self.root:
            self._save_geometry()

    # -- loops ---------------------------------------------------------------

    def _pointer_inside(self) -> bool:
        x, y = self.root.winfo_pointerx(), self.root.winfo_pointery()
        left, top = self.root.winfo_x(), self.root.winfo_y()
        return left <= x < left + self.root.winfo_width() and top <= y < top + self.root.winfo_height()

    def _animate(self) -> None:
        try:
            winui.track_foreground(self.root)
            self._hover = self._pointer_inside()
            target = float(self.settings["alpha_hover"]) if (self._hover or self._locked) else self._idle_alpha
            delta = target - self._alpha
            if abs(delta) > 0.004:
                self._alpha += max(-ALPHA_STEP, min(ALPHA_STEP, delta))
                self.root.attributes("-alpha", round(self._alpha, 3))
                self.root.after(ANIM_MS, self._animate)
                return
            if self._alpha != target:
                self._alpha = target
                self.root.attributes("-alpha", target)
            blink = int(time.time() * 1000) // BLINK_MS % 2 == 0
            if blink != self._blink:
                self._blink = blink
                for card in self.cards:
                    if card.needs_attention and card.winfo_ismapped():
                        card.paint_dot(state_color(card.state), dim=not blink)
        except tk.TclError:
            return
        self.root.after(ANIM_MS, self._animate)

    def _poll(self) -> None:
        started = time.perf_counter()
        try:
            self.sessions = sources.scan_all()
        finally:
            self._scan_ms = (time.perf_counter() - started) * 1000
        self._render()
        self.root.after(int(self.settings["poll_interval_ms"]), self._poll)

    # -- rendering -----------------------------------------------------------

    def _needed_height(self, count: int) -> int:
        """Design pixels: header + one row per session + footer."""
        return HEADER_H + 3 + count * CARD_H + FOOTER_H + 6

    def _render(self) -> None:
        width, height = self.root.winfo_width(), self.root.winfo_height()
        limit = int(self.settings["max_cards"])
        shown = self.sessions[:limit]

        if self._auto_height:
            wanted = winui.px(max(int(self.settings["min_height"]), self._needed_height(len(shown))))
            if wanted != height:
                self._resize(width, wanted)
                self.root.update_idletasks()
                width, height = self.root.winfo_width(), self.root.winfo_height()

        card_w = width - winui.px(6)
        multi_source = len({s.source for s in shown}) > 1
        while len(self.cards) < len(shown):
            self.cards.append(Card(self.body, self.fonts, self._activate))

        for i, card in enumerate(self.cards):
            if i >= len(shown):
                card.place_forget()
                continue
            card.place(x=0, y=winui.px(i * CARD_H), relwidth=1.0, height=winui.px(CARD_H - 4))
            session = shown[i]
            # Cards are reused by slot, so the jump target follows every render.
            card.target = session
            if card.signature != session.signature() or card.box != card_w:
                card.show(session, card_w, tag=session.source if multi_source else "")
                card.signature = session.signature()
                card.box = card_w
            elif card.needs_attention:
                card.paint_dot(state_color(session.state), dim=not self._blink)

        text = i18n.t("footer_sessions", n=len(self.sessions), ms=self._scan_ms)
        hidden = len(self.sessions) - len(shown)
        if hidden > 0:
            text += i18n.t("footer_hidden", n=hidden)
        if sources.ERRORS:
            text = i18n.t("footer_adapter_error") + " ".join(f"{k}:{v}" for k, v in sources.ERRORS.items())
            self.footer.configure(fg=WARN)
        else:
            self.footer.configure(fg=MUTED)
            held, since = self._held
            if held and time.time() - since < FOOTER_HOLD:
                text = held
        self._footer(text)

    def _footer(self, text: str) -> None:
        self.footer.configure(
            text=clip(text, self.root.winfo_width() - winui.px(34), self.fonts["small"].measure)
        )

    def run(self) -> None:
        self.root.mainloop()


def render_text(sessions: list[AgentSession]) -> str:
    lines = []
    for s in sessions:
        alive = "" if s.alive else i18n.t("text_exited")
        lines.append(
            f"[{state_label(s.state)}] {s.project} pid={s.pid}{alive}"
            f"{i18n.t('text_uptime')}{fmt_duration(s.uptime())}"
        )
        if s.prompt:
            lines.append(i18n.t("text_request") + s.prompt)
        lines.append(f"    {s.detail}")
        if s.tasks_total:
            lines.append(i18n.t("text_task") + f"{s.tasks_done}/{s.tasks_total} {s.current_task}")
        tail = f" {s.usage}" if s.usage else ""
        lines.append(
            i18n.t("text_counts", turns=s.turns, tools=s.tool_calls, errors=s.errors)
            + tail + i18n.t("text_branch") + (s.branch or "-")
            + i18n.t("text_model") + (s.model or "-")
        )
        for act in s.activity:
            lines.append(f"      {act}")
    if not sessions:
        lines.append(i18n.t("text_no_sessions"))
    if sources.ERRORS:
        lines.append(i18n.t("text_adapter_error") + " ".join(f"{k}={v}" for k, v in sources.ERRORS.items()))
    return "\n".join(lines)


def _utf8_stdout() -> None:
    try:  # Windows consoles start on a legacy code page
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def main(argv: list[str]) -> int:
    settings = config.load()
    i18n.init()
    parser = argparse.ArgumentParser(description=i18n.t("cli_desc"))
    parser.add_argument("--once", action="store_true", help=i18n.t("cli_once"))
    parser.add_argument("--repeat", action="store_true", help=i18n.t("cli_repeat"))
    parser.add_argument("--interval-ms", type=int, dest="interval", help=i18n.t("cli_interval"))
    parser.add_argument(
        "--home",
        action="append",
        default=[],
        help=i18n.t("cli_home"),
    )
    args = parser.parse_args(argv)
    if args.interval:
        settings["poll_interval_ms"] = args.interval
    if args.home:
        # Every adapter names its key "<source>_homes" and probes its own marker inside the
        # directory, so one flag feeds them all and board.py still knows nothing about any CLI.
        for key in [k for k in settings if k.endswith("_homes")]:
            settings[key] = list(args.home) + list(settings.get(key) or [])

    if args.once:
        _utf8_stdout()
        while True:
            print("\n" + render_text(sources.scan_all()), flush=True)
            if not args.repeat:
                break
            time.sleep(settings["poll_interval_ms"] / 1000.0)
        return 0

    try:
        Board().run()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
