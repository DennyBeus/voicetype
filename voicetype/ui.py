"""Окно правки и звуковая индикация.

Окно — не «подтвердите текст», а полноценный мини-редактор: сюда можно
вставить ссылку из буфера, поправить путь, удалить лишнее. Всё то, что
голосом диктовать бессмысленно.

Enter вставляет, поэтому перенос строки — Shift+Enter.
"""

from __future__ import annotations

import json
import logging
import math
import struct
import subprocess
import tkinter as tk
import wave
from pathlib import Path
from tkinter import font as tkfont

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".state"

BG = "#1e1e24"
FG = "#e6e6ea"
DIM = "#7b7b88"
ACCENT = "#7aa2f7"
ERR = "#f7768e"


# ── звук ────────────────────────────────────────────────────────────────
class Beeper:
    """Короткие сигналы старта, стопа и ошибки.

    Файлы генерируются один раз при первом запуске — это дешевле, чем
    тянуть зависимость ради трёх писков.
    """

    TONES = {
        "start": (660.0, 0.07),
        "stop": (440.0, 0.07),
        "error": (220.0, 0.22),
    }

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._paths: dict[str, Path] = {}
        if enabled:
            self._ensure_files()

    def _ensure_files(self) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        for name, (freq, secs) in self.TONES.items():
            path = STATE_DIR / f"beep-{name}.wav"
            if not path.exists():
                _write_tone(path, freq, secs)
            self._paths[name] = path

    def play(self, name: str) -> None:
        if not self.enabled:
            return
        path = self._paths.get(name)
        if not path:
            return
        try:
            subprocess.Popen(
                ["pw-play", str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            log.debug("pw-play не найден — сигналы отключены")
            self.enabled = False


def _write_tone(path: Path, freq: float, secs: float, rate: int = 48000) -> None:
    frames = int(rate * secs)
    fade = max(1, frames // 8)  # без затухания щёлкает
    samples = []
    for i in range(frames):
        env = min(1.0, i / fade, (frames - i) / fade)
        samples.append(int(0.28 * env * 32767 * math.sin(2 * math.pi * freq * i / rate)))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))


# ── окно правки ─────────────────────────────────────────────────────────
class ReviewWindow:
    """Модальное окно поверх всех. Живёт до Enter или Esc."""

    def __init__(self, tk_root: tk.Misc, cfg: dict):
        self._root = tk_root
        self._cfg = cfg
        self._win: tk.Toplevel | None = None
        self._text: tk.Text | None = None
        self._raw = ""
        self._edited = ""
        self._showing_raw = False
        self._on_done = None

    @property
    def open(self) -> bool:
        return self._win is not None

    def show(self, text: str, *, previous: str = "", raw: str = "",
             note: str | None = None, on_done=None) -> None:
        """on_done(text|None, reset_session: bool) — None означает отмену."""
        self._raw = raw or text
        self._edited = text
        self._showing_raw = False
        self._on_done = on_done

        geom = _load_geometry(self._cfg)
        win = tk.Toplevel(self._root)
        self._win = win
        win.title("voicetype")
        win.configure(bg=BG)
        win.geometry(geom)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self._cancel)

        size = int(self._cfg.get("font_size", 12))
        family = str(self._cfg.get("font_family", "")).strip()
        body_font = _ui_font(size, family)
        small = _ui_font(max(8, size - 3), family)

        if previous:
            head = tk.Label(
                win, text=_shorten(previous), bg=BG, fg=DIM, font=small,
                anchor="w", justify="left", wraplength=1200, padx=10, pady=6,
            )
            head.pack(fill="x")
            tk.Frame(win, bg=DIM, height=1).pack(fill="x", padx=10)

        body = tk.Text(
            win, bg=BG, fg=FG, insertbackground=ACCENT,
            font=body_font, wrap="word", undo=True, maxundo=-1,
            relief="flat", padx=16, pady=12, selectbackground=ACCENT,
            selectforeground=BG, highlightthickness=0,
            # Воздух между строками — половина работы по читаемости
            # длинной надиктованной фразы.
            spacing1=2, spacing2=4, spacing3=2,
            insertwidth=2,
        )
        body.pack(fill="both", expand=True)
        body.insert("1.0", text)
        body.edit_reset()          # начальная вставка не должна отменяться
        body.mark_set("insert", "end-1c")
        self._text = body

        hint = "Enter — вставить   Shift+Enter — перенос   Esc — отмена   Ctrl+R — сырой текст"
        if note:
            hint = f"⚠ {note}    " + hint
        tk.Label(
            win, text=hint, bg=BG, fg=ERR if note else DIM, font=small,
            anchor="w", padx=10, pady=5,
        ).pack(fill="x")

        self._bind(body, win)
        _center(win)
        win.deiconify()
        win.lift()
        win.focus_force()
        body.focus_set()

    def append_dictation(self, addition: str) -> None:
        """Дописать в открытое окно то, что надиктовали поверх."""
        if not self._text:
            return
        current = self._text.get("1.0", "end-1c")
        glue = "" if not current or current.endswith((" ", "\n")) else " "
        self._text.insert("end-1c", glue + addition)
        self._text.mark_set("insert", "end-1c")
        self._text.see("end-1c")
        self._win.lift()
        self._win.focus_force()
        self._text.focus_set()

    # ── горячие клавиши ─────────────────────────────────────────────────
    def _bind(self, body: tk.Text, win: tk.Toplevel) -> None:
        body.bind("<Return>", self._accept)
        body.bind("<Shift-Return>", lambda e: None)  # штатный перенос строки
        body.bind("<KP_Enter>", self._accept)
        body.bind("<Escape>", self._cancel)
        body.bind("<Control-Escape>", lambda e: self._cancel(reset=True))
        body.bind("<Control-r>", self._toggle_raw)
        body.bind("<Control-a>", self._select_all)
        body.bind("<Control-slash>", self._select_all)
        # Tk по умолчанию вешает redo на Ctrl+Y; добавляем привычный вариант.
        body.bind("<Control-Shift-Z>", self._redo)
        body.bind("<Control-w>", self._delete_word)
        win.bind("<Configure>", self._remember_geometry)

    def _accept(self, _event=None) -> str:
        if not self._text:
            return "break"
        value = self._text.get("1.0", "end-1c").strip()
        self._finish(value or None, reset=False)
        return "break"

    def _cancel(self, _event=None, reset: bool = False) -> str:
        self._finish(None, reset=reset)
        return "break"

    def _toggle_raw(self, _event=None) -> str:
        if not self._text:
            return "break"
        if self._showing_raw:
            new = self._edited
        else:
            self._edited = self._text.get("1.0", "end-1c")
            new = self._raw
        self._showing_raw = not self._showing_raw
        self._text.delete("1.0", "end")
        self._text.insert("1.0", new)
        self._text.mark_set("insert", "end-1c")
        return "break"

    def _select_all(self, _event=None) -> str:
        if self._text:
            self._text.tag_add("sel", "1.0", "end-1c")
            self._text.mark_set("insert", "end-1c")
        return "break"

    def _redo(self, _event=None) -> str:
        try:
            self._text.edit_redo()
        except tk.TclError:
            pass
        return "break"

    def _delete_word(self, _event=None) -> str:
        if self._text:
            self._text.delete("insert-1c wordstart", "insert")
        return "break"

    def _remember_geometry(self, event) -> None:
        if self._win is not None and event.widget is self._win:
            _save_geometry(event.width, event.height)

    def _finish(self, value: str | None, reset: bool) -> None:
        win, callback = self._win, self._on_done
        self._win = self._text = None
        self._on_done = None
        if win is not None:
            win.destroy()
        if callback:
            callback(value, reset)


# ── мелочи ──────────────────────────────────────────────────────────────
# Порядок предпочтения. Первый доступный и берём.
FONT_CHAIN = (
    "Ubuntu Sans",      # системный шрифт GNOME на Ubuntu
    "Cantarell",        # системный шрифт GNOME вообще
    "Noto Sans",
    "DejaVu Sans",
    "Liberation Sans",
    "FreeSans",
)


def _ui_font(size: int, family: str = ""):
    """Обычный текстовый шрифт системы.

    Диктуют здесь связную речь, а не код, поэтому моноширинный мешает читать.

    Подбор не сводится к TkTextFont: если Tk собран без Xft (так собран
    интерпретатор от uv), он не видит fontconfig и подставляет древний
    `nimbus sans l` — без сглаживания и с посредственной кириллицей.
    Поэтому идём по списку живых системных шрифтов, а на TkTextFont
    падаем только если ни одного из них нет.
    """
    available = {f.lower() for f in tkfont.families()}
    chain = (family,) + FONT_CHAIN if family else FONT_CHAIN

    for candidate in chain:
        if candidate and candidate.lower() in available:
            return tkfont.Font(family=candidate, size=size)

    font = tkfont.nametofont("TkTextFont").copy()
    font.configure(size=size)
    return font


def fonts_are_degraded() -> bool:
    """True, если Tk не видит ни одного нормального системного шрифта."""
    available = {f.lower() for f in tkfont.families()}
    return not any(f.lower() in available for f in FONT_CHAIN)


def _shorten(text: str, limit: int = 220) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else "…" + text[-limit:]


def _center(win: tk.Toplevel) -> None:
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    x = (win.winfo_screenwidth() - w) // 2
    y = int(win.winfo_screenheight() * 0.32)
    win.geometry(f"+{x}+{y}")


def _geometry_file() -> Path:
    return STATE_DIR / "window.json"


def _load_geometry(cfg: dict) -> str:
    width, height = int(cfg.get("width", 720)), int(cfg.get("height", 260))
    try:
        saved = json.loads(_geometry_file().read_text())
        width, height = int(saved["width"]), int(saved["height"])
    except (OSError, ValueError, KeyError):
        pass
    return f"{width}x{height}"


def _save_geometry(width: int, height: int) -> None:
    if width < 200 or height < 100:
        return
    try:
        STATE_DIR.mkdir(exist_ok=True)
        _geometry_file().write_text(json.dumps({"width": width, "height": height}))
    except OSError:
        pass
