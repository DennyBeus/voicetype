import tkinter as tk

import pytest
from Xlib import X, Xatom

from voicetype.inject import InjectError, Injector


@pytest.mark.x11
@pytest.mark.parametrize("wm_class,is_terminal", [
    ("Code", True), ("com.microsoft.VSCode", True),
    ("Gnome-terminal", True), ("Firefox", False),
])
def test_x11_text_paste_and_clipboard_restore(x11_display, monkeypatch, wm_class, is_terminal):
    monkeypatch.setenv("DISPLAY", x11_display)
    owner = tk.Tk(screenName=x11_display)
    owner.withdraw()
    consumer = tk.Tk(screenName=x11_display, className=wm_class)
    body = tk.Text(consumer)
    body.pack()
    consumer.update()
    consumer.focus_force()
    body.focus_set()
    consumer.clipboard_clear()
    consumer.clipboard_append("Сохранённый буфер Ω")
    consumer.update()
    injector = Injector({"terminal_classes": ["gnome-terminal"]}, owner)
    received = []
    errors = []

    def paste(event):
        received.append("terminal-paste" if event.state & X.ShiftMask else "text-paste")
        body.insert("insert", consumer.clipboard_get())
        return "break"

    def image_paste(event):
        received.append("image-paste")
        return "break"

    owner.report_callback_exception = lambda *args: errors.append(args)
    consumer.report_callback_exception = owner.report_callback_exception
    body.bind("<Control-Shift-V>", paste)
    body.bind("<Control-v>", image_paste if is_terminal else paste)
    try:
        win = injector._d.create_resource_object("window", consumer.winfo_id()).query_tree().parent
        injector._root.change_property(injector._net_active, Xatom.WINDOW, 32, [win.id])
        injector._d.sync()
        target = injector.snapshot()
        assert target is not None
        assert target.is_terminal is is_terminal
        text = "Проверь Codex CLI: API, путь `/tmp/test`.\nВторая строка — без отправки."

        injector.paste(text, target)
        owner.after(1000, owner.quit)
        owner.mainloop()

        assert errors == []
        assert body.get("1.0", "end-1c") == text
        assert received == ["terminal-paste" if is_terminal else "text-paste"]
        assert consumer.clipboard_get() == "Сохранённый буфер Ω"
    finally:
        injector._d.close()
        consumer.destroy()
        owner.destroy()


@pytest.mark.x11
def test_type_fallback_restores_keyboard_mapping(x11_display, monkeypatch):
    monkeypatch.setenv("DISPLAY", x11_display)
    root = tk.Tk(screenName=x11_display)
    injector = Injector({"method": "type"}, root)
    try:
        spare = injector._find_spare_keycode()
        assert spare is not None
        original = list(injector._d.get_keyboard_mapping(spare, 1)[0])
        injector._type("a\nЖ🙂")
        assert list(injector._d.get_keyboard_mapping(spare, 1)[0]) == original
        monkeypatch.setattr(injector, "_find_spare_keycode", lambda: None)
        with pytest.raises(InjectError, match="keycode"):
            injector._type("text")
    finally:
        injector._d.close()
        root.destroy()
