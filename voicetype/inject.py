"""Возврат фокуса и вставка текста в окно, где стоял курсор.

Вставляем через буфер обмена, а не посимвольным набором: XTEST для кириллицы
требует временного перемапливания keycode'ов и на практике теряет символы.

Буфер при этом чужой — в него пользователь мог только что скопировать ссылку,
чтобы вставить её в окне правки. Поэтому владеем им ровно на время вставки:
сохранили → положили своё → вставили → вернули как было.
"""

from __future__ import annotations

import logging
import time

from Xlib import X, XK, display, protocol

log = logging.getLogger(__name__)

# Сколько ждать, пока приложение заберёт содержимое буфера, прежде чем
# возвращать пользовательское. Меньше — начинаются гонки на медленных окнах.
PASTE_SETTLE_S = 0.5
# Пауза между возвратом фокуса и нажатием: оконный менеджер переключает
# фокус не мгновенно, а клавиши уйдут туда, где фокус на момент отправки.
FOCUS_SETTLE_S = 0.15


class InjectError(RuntimeError):
    pass


class Target:
    """Окно, куда вернёмся вставлять."""

    __slots__ = ("window_id", "wm_class", "is_terminal")

    def __init__(self, window_id: int, wm_class: str, is_terminal: bool):
        self.window_id = window_id
        self.wm_class = wm_class
        self.is_terminal = is_terminal

    def __repr__(self) -> str:
        kind = "терминал" if self.is_terminal else "окно"
        return f"<{kind} {self.wm_class} id=0x{self.window_id:x}>"


class Injector:
    def __init__(self, cfg: dict, tk_root):
        self.method = cfg.get("method", "clipboard")
        self.terminal_classes = {
            c.lower() for c in cfg.get("terminal_classes", [])
        }
        self._tk = tk_root
        self._d = display.Display()
        self._root = self._d.screen().root
        self._net_active = self._d.intern_atom("_NET_ACTIVE_WINDOW")

        self._kc_ctrl = self._d.keysym_to_keycode(XK.XK_Control_L)
        self._kc_shift = self._d.keysym_to_keycode(XK.XK_Shift_L)
        self._kc_v = self._d.keysym_to_keycode(XK.XK_v)

    # ── кто сейчас в фокусе ─────────────────────────────────────────────
    def snapshot(self) -> Target | None:
        """Запомнить активное окно до того, как мы покажем своё."""
        try:
            prop = self._root.get_full_property(self._net_active, X.AnyPropertyType)
            if not prop or not prop.value:
                return None
            window_id = int(prop.value[0])
            if not window_id:
                return None
            wm_class = self._wm_class(window_id)
        except Exception as exc:  # noqa: BLE001 — X может отдать что угодно
            log.warning("не удалось определить активное окно: %s", exc)
            return None

        return Target(
            window_id=window_id,
            wm_class=wm_class,
            is_terminal=self._is_terminal(wm_class),
        )

    def _is_terminal(self, wm_class: str) -> bool:
        """Терминалам нужен Ctrl+Shift+V, остальным — Ctrl+V.

        Сравниваем по вхождению, а не по равенству: один и тот же терминал
        зовётся то `Gnome-terminal`, то `gnome-terminal-server`, то
        `org.wezfurlong.wezterm` — точный список тут не собрать.
        """
        cls = wm_class.lower()
        if not cls:
            return False
        return any(
            known == cls or known in cls or cls in known
            for known in self.terminal_classes
        )

    def _wm_class(self, window_id: int) -> str:
        try:
            win = self._d.create_resource_object("window", window_id)
            pair = win.get_wm_class()
        except Exception:  # noqa: BLE001
            return ""
        if not pair:
            return ""
        # get_wm_class отдаёт (instance, class); класс информативнее.
        return pair[1] or pair[0] or ""

    # ── вставка ─────────────────────────────────────────────────────────
    def paste(self, text: str, target: Target | None) -> None:
        """Положить текст в целевое окно.

        Шаги разнесены через Tk-таймер, а не через sleep, и это не косметика.
        Владелец буфера обмена — наш Tk, и отдать содержимое терминалу он
        может только ответив на SelectionRequest из своего цикла событий.
        Если между «положил в буфер» и «нажал Ctrl+Shift+V» заснуть прямо
        в главном потоке, цикл замирает, ответить некому — и терминал
        вставляет пустоту, хотя в логах всё выглядит успешным.
        """
        if not text:
            return
        if self.method == "type":
            self._focus(target)
            self._type(text)
            return

        saved = self._clipboard_get()
        self._clipboard_set(text)
        self._focus(target)

        combo = [self._kc_ctrl]
        if target is not None and target.is_terminal:
            combo.append(self._kc_shift)

        def send_keys() -> None:
            try:
                self._send(combo, self._kc_v)
            except Exception as exc:  # noqa: BLE001
                log.error("не удалось отправить сочетание вставки: %s", exc)
            finally:
                # Буфер возвращаем с запасом по времени: приложение забирает
                # содержимое асинхронно, уже после нажатия.
                self._tk.after(int(PASTE_SETTLE_S * 1000), restore)

        def restore() -> None:
            if saved is not None:
                try:
                    self._clipboard_set(saved)
                except InjectError as exc:
                    log.warning("не удалось вернуть буфер обмена: %s", exc)

        self._tk.after(int(FOCUS_SETTLE_S * 1000), send_keys)

    def _focus(self, target: Target | None) -> None:
        if target is None:
            return
        try:
            win = self._d.create_resource_object("window", target.window_id)
            event = protocol.event.ClientMessage(
                window=win,
                client_type=self._net_active,
                data=(32, [2, X.CurrentTime, 0, 0, 0]),  # 2 = запрос от пейджера
            )
            mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
            self._root.send_event(event, event_mask=mask)
            self._d.sync()
            win.set_input_focus(X.RevertToParent, X.CurrentTime)
            self._d.sync()
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось вернуть фокус на %r: %s", target, exc)

    def _send(self, modifiers: list[int], keycode: int) -> None:
        from Xlib.ext import xtest

        for mod in modifiers:
            xtest.fake_input(self._d, X.KeyPress, mod)
        xtest.fake_input(self._d, X.KeyPress, keycode)
        xtest.fake_input(self._d, X.KeyRelease, keycode)
        for mod in reversed(modifiers):
            xtest.fake_input(self._d, X.KeyRelease, mod)
        self._d.sync()

    # ── буфер обмена (через Tk — он умеет владеть селекцией) ────────────
    def _clipboard_get(self) -> str | None:
        try:
            return self._tk.clipboard_get()
        except Exception:  # noqa: BLE001 — пусто или не текст (картинка)
            return None

    def _clipboard_set(self, text: str) -> None:
        try:
            self._tk.clipboard_clear()
            self._tk.clipboard_append(text)
            self._tk.update_idletasks()
        except Exception as exc:  # noqa: BLE001
            raise InjectError(f"не удалось положить текст в буфер: {exc}") from exc

    # ── запасной посимвольный набор ─────────────────────────────────────
    def _type(self, text: str) -> None:
        """Набор через временное перемапливание свободного keycode.

        Запасной путь для окон, где вставка из буфера не работает. Медленнее
        и не любит быстрый набор, поэтому по умолчанию не используется.
        """
        from Xlib.ext import xtest

        spare = self._find_spare_keycode()
        if spare is None:
            raise InjectError("нет свободного keycode для посимвольного набора")

        original = self._d.get_keyboard_mapping(spare, 1)[0]
        try:
            for char in text:
                keysym = XK.string_to_keysym(_keysym_name(char))
                if keysym == X.NoSymbol:
                    keysym = ord(char) + 0x01000000  # Unicode-keysym
                self._d.change_keyboard_mapping(spare, [[keysym] * len(original)])
                self._d.sync()
                xtest.fake_input(self._d, X.KeyPress, spare)
                xtest.fake_input(self._d, X.KeyRelease, spare)
                self._d.sync()
                time.sleep(0.004)
        finally:
            self._d.change_keyboard_mapping(spare, [original])
            self._d.sync()

    def _find_spare_keycode(self) -> int | None:
        min_kc = self._d.display.info.min_keycode
        max_kc = self._d.display.info.max_keycode
        mapping = self._d.get_keyboard_mapping(min_kc, max_kc - min_kc + 1)
        for offset, syms in enumerate(mapping):
            if not any(syms):
                return min_kc + offset
        return None


def _keysym_name(char: str) -> str:
    """Имя keysym для ASCII; для остального вернём сам символ."""
    special = {
        " ": "space", "\n": "Return", "\t": "Tab", "-": "minus",
        "=": "equal", "[": "bracketleft", "]": "bracketright",
        ";": "semicolon", "'": "apostrophe", ",": "comma", ".": "period",
        "/": "slash", "\\": "backslash", "`": "grave",
    }
    return special.get(char, char)
