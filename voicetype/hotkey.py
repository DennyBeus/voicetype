"""Глобальный перехват клавиши-триггера через RECORD-расширение X11.

RECORD видит события до того, как их получит приложение с фокусом, и не
требует grab — поэтому клавиша продолжает работать как обычно везде, где
мы её не используем. Это же заставляет схему работать под
gnome-remote-desktop: он вбрасывает события через XTEST в тот же X-сервер,
а RECORD их видит.

Одна клавиша даёт два режима:
  зажал дольше порога → push-to-talk, отпустил — конец записи;
  коротко нажал       → toggle, запись идёт до следующего нажатия.
"""

from __future__ import annotations

import logging
import threading
import time

from Xlib import X, XK, display
from Xlib.ext import record
from Xlib.protocol import rq

log = logging.getLogger(__name__)

KEYSYMS = {
    "ctrl_r": XK.XK_Control_R,
    "ctrl_l": XK.XK_Control_L,
    "alt_r": XK.XK_Alt_R,
    "super_r": XK.XK_Super_R,
    "f13": XK.XK_F13,
    "f14": XK.XK_F14,
    "pause": XK.XK_Pause,
    "menu": XK.XK_Menu,
    "scroll_lock": XK.XK_Scroll_Lock,
}

IDLE, HOLD, TOGGLE = "idle", "hold", "toggle"


class HotkeyError(RuntimeError):
    pass


class HotkeyListener:
    """Слушает клавишу и зовёт on_start / on_stop. Работает в своём потоке."""

    def __init__(self, cfg: dict, on_start, on_stop):
        name = str(cfg.get("key", "ctrl_r")).lower()
        if name not in KEYSYMS:
            raise HotkeyError(
                f"неизвестная клавиша {name!r}; доступны: {', '.join(sorted(KEYSYMS))}"
            )
        self.key_name = name
        self.threshold = int(cfg.get("hold_threshold_ms", 400)) / 1000.0
        self.on_start = on_start
        self.on_stop = on_stop

        self._local = display.Display()
        self._record = display.Display()
        if not self._record.has_extension("RECORD"):
            raise HotkeyError(
                "X-сервер без расширения RECORD — перехват клавиш невозможен"
            )

        self._keycode = self._local.keysym_to_keycode(KEYSYMS[name])
        if not self._keycode:
            raise HotkeyError(f"клавиша {name!r} отсутствует в текущей раскладке")

        self._state = IDLE
        self._down_at = 0.0
        self._physically_down = False
        self._lock = threading.Lock()
        self._ctx = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._ctx = self._record.record_create_context(
            0,
            [record.AllClients],
            [{
                "core_requests": (0, 0),
                "core_replies": (0, 0),
                "ext_requests": (0, 0, 0, 0),
                "ext_replies": (0, 0, 0, 0),
                "delivered_events": (0, 0),
                "device_events": (X.KeyPress, X.KeyRelease),
                "errors": (0, 0),
                "client_started": False,
                "client_died": False,
            }],
        )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("слушаю клавишу %s (порог удержания %.0f мс)",
                 self.key_name, self.threshold * 1000)

    def _run(self) -> None:
        try:
            self._record.record_enable_context(self._ctx, self._handle)
        except Exception:  # noqa: BLE001 — поток-демон, падать молча нельзя
            log.exception("слушатель клавиш упал")
        finally:
            if self._ctx:
                self._record.record_free_context(self._ctx)

    def stop(self) -> None:
        if self._ctx:
            self._local.record_disable_context(self._ctx)
            self._local.flush()

    def _handle(self, reply) -> None:
        if reply.category != record.FromServer or reply.client_swapped:
            return
        if not len(reply.data) or reply.data[0] < 2:
            return

        data = reply.data
        while len(data):
            event, data = rq.EventField(None).parse_binary_value(
                data, self._record.display, None, None
            )
            if event.detail != self._keycode:
                continue
            if event.type == X.KeyPress:
                self._on_press()
            elif event.type == X.KeyRelease:
                self._on_release()

    # ── машина состояний ────────────────────────────────────────────────
    def _on_press(self) -> None:
        with self._lock:
            if self._physically_down:
                return  # автоповтор — игнорируем
            self._physically_down = True

            if self._state == TOGGLE:
                # Второе нажатие в toggle-режиме закрывает запись.
                self._state = IDLE
                fire = self.on_stop
            else:
                self._down_at = time.monotonic()
                self._state = HOLD
                fire = self.on_start
        self._safe(fire)

    def _on_release(self) -> None:
        with self._lock:
            self._physically_down = False
            if self._state != HOLD:
                return
            held = time.monotonic() - self._down_at
            if held >= self.threshold:
                self._state = IDLE
                fire = self.on_stop
            else:
                # Короткий тап — остаёмся писать до следующего нажатия.
                self._state = TOGGLE
                fire = None
        if fire:
            self._safe(fire)

    def reset(self) -> None:
        """Сбросить состояние, если запись оборвалась не через клавишу."""
        with self._lock:
            self._state = IDLE

    @staticmethod
    def _safe(fn) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("обработчик хоткея упал")
