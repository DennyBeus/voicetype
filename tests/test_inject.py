from unittest.mock import Mock

import pytest
from Xlib import XK

from voicetype.inject import InjectError, Injector, Target


class ClipboardLoop:
    def __init__(self, text="original clipboard"):
        self.text = text
        self.pending = []

    def clipboard_get(self):
        if self.text is None:
            raise RuntimeError("no text")
        return self.text

    def clipboard_clear(self):
        self.text = ""

    def clipboard_append(self, text):
        self.text += text

    def update_idletasks(self):
        pass

    def after(self, delay, callback):
        self.pending.append(callback)

    def advance(self):
        self.pending.pop(0)()


@pytest.fixture
def make_injector(monkeypatch):
    connection = Mock()
    connection.keysym_to_keycode.side_effect = lambda sym: sym
    monkeypatch.setattr("voicetype.inject.display.Display", lambda: connection)

    def make(cfg=None):
        loop = ClipboardLoop()
        injector = Injector(cfg or {"terminal_classes": ["gnome-terminal"]}, loop)
        injector._focus = Mock()
        injector._send = Mock()
        return injector, loop

    return make


@pytest.mark.parametrize("wm_class", [
    "Gnome-terminal", "gnome-terminal-server", "Code", "code-oss",
    "Code - Insiders", "VSCodium", "Cursor", "Windsurf",
])
def test_terminal_and_ide_paste_does_not_send_codex_image_shortcut(make_injector, wm_class):
    injector, loop = make_injector()
    target = Target(42, wm_class, injector._is_terminal(wm_class))
    text = "Исправь Codex CLI.\nKeep API and `paths` intact."

    injector.paste(text, target)

    assert loop.text == text
    injector._send.assert_not_called()
    loop.advance()
    injector._send.assert_called_once_with([XK.XK_Control_L, XK.XK_Shift_L], XK.XK_v)
    assert loop.text == text
    loop.advance()
    assert loop.text == "original clipboard"


@pytest.mark.parametrize("wm_class", ["firefox", "Gedit", "my-code-browser", "", "term"])
def test_ordinary_windows_keep_ctrl_v(make_injector, wm_class):
    injector, loop = make_injector()
    target = Target(42, wm_class, injector._is_terminal(wm_class))

    injector.paste("text", target)
    loop.advance()

    injector._send.assert_called_once_with([XK.XK_Control_L], XK.XK_v)


@pytest.mark.parametrize("shortcut,terminal,modifiers", [
    ("ctrl+shift+v", False, [XK.XK_Control_L, XK.XK_Shift_L]),
    ("ctrl+v", True, [XK.XK_Control_L]),
])
def test_explicit_shortcut_overrides_detection(make_injector, shortcut, terminal, modifiers):
    injector, loop = make_injector({"paste_shortcut": shortcut})
    injector.paste("text", Target(42, "custom", terminal))
    loop.advance()
    injector._send.assert_called_once_with(modifiers, XK.XK_v)


def test_ide_detection_can_be_disabled_for_editor_use(make_injector):
    injector, _ = make_injector({"terminal_editor_classes": []})
    assert not injector._is_terminal("code")


def test_custom_ide_class_is_matched_exactly(make_injector):
    injector, _ = make_injector({"terminal_editor_classes": ["MyIDE"]})
    assert injector._is_terminal("myide")
    assert not injector._is_terminal("myide-browser")


@pytest.mark.parametrize("cfg", [{"paste_shortcut": "ctrl+x"}, {"method": "invalid"}])
def test_invalid_settings_fail_at_startup(make_injector, cfg):
    with pytest.raises(InjectError):
        make_injector(cfg)


def test_missing_target_does_not_send_keys_or_change_clipboard(make_injector):
    injector, loop = make_injector()
    with pytest.raises(InjectError):
        injector.paste("text", None)
    assert loop.text == "original clipboard"
    assert not loop.pending
    injector._send.assert_not_called()


def test_empty_text_is_noop(make_injector):
    injector, loop = make_injector()
    injector.paste("", None)
    assert not loop.pending
    injector._focus.assert_not_called()


def test_paste_keeps_working_with_empty_or_image_clipboard(make_injector):
    injector, loop = make_injector()
    loop.text = None
    injector.paste("Новый текст", Target(42, "Code", True))
    loop.advance()
    loop.advance()
    assert loop.text == "Новый текст"


def test_failed_shortcut_still_restores_clipboard(make_injector, caplog):
    injector, loop = make_injector()
    injector._send.side_effect = RuntimeError("XTEST failed")
    injector.paste("text", Target(42, "Code", True))
    loop.advance()
    loop.advance()
    assert loop.text == "original clipboard"
    assert "XTEST failed" in caplog.text


def test_type_method_remains_available(make_injector):
    injector, loop = make_injector({"method": "type"})
    injector._type = Mock()
    target = Target(42, "Code", True)
    injector.paste("Привет", target)
    injector._type.assert_called_once_with("Привет")
    assert loop.text == "original clipboard"
    assert not loop.pending
