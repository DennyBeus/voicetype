import os
import shutil
import signal
import subprocess
import sys
import time
import tkinter as tk

import pytest
from Xlib import X, Xatom

from voicetype.inject import Injector


@pytest.mark.x11
def test_real_terminal_receives_bracketed_utf8_paste(
    x11_display, x11_window_manager, monkeypatch, tmp_path,
):
    """Read terminal input without running a shell command or submitting a prompt."""
    if not all(shutil.which(tool) for tool in ("xfce4-terminal", "dbus-run-session")):
        pytest.skip("xfce4-terminal and dbus-run-session are required")
    monkeypatch.setenv("DISPLAY", x11_display)
    config_dir = tmp_path / "config"
    terminal_config = config_dir / "xfce4" / "terminal" / "terminalrc"
    terminal_config.parent.mkdir(parents=True)
    terminal_config.write_text("[Configuration]\nMiscShowUnsafePasteDialog=FALSE\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    reader = tmp_path / "reader.py"
    ready = tmp_path / "ready"
    output = tmp_path / "input.bin"
    reader.write_text(
        "import os, pathlib, select, sys, time, tty\n"
        "tty.setraw(0)\n"
        "os.write(1, b'\\x1b[?2004h')\n"
        "pathlib.Path(sys.argv[1]).touch()\n"
        "data = b''\n"
        "deadline = time.monotonic() + 8\n"
        "while time.monotonic() < deadline:\n"
        "    if select.select([0], [], [], 0.05)[0]:\n"
        "        data += os.read(0, 65536)\n"
        "        pathlib.Path(sys.argv[2]).write_bytes(data)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        ["dbus-run-session", "--", "xfce4-terminal", "--disable-server", "--execute",
         sys.executable, str(reader), str(ready), str(output)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    root = tk.Tk(screenName=x11_display)
    root.withdraw()
    injector = Injector({"terminal_classes": ["xfce4-terminal"]}, root)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert ready.exists(), "terminal input reader did not start"
        clients = injector._root.get_full_property(
            injector._d.intern_atom("_NET_CLIENT_LIST"), Xatom.WINDOW,
        )
        assert clients is not None
        windows = [injector._d.create_resource_object("window", int(wid)) for wid in clients.value]
        window = next(win for win in windows if win.get_attributes().map_state == X.IsViewable
                      and win.get_wm_class() and
                      "xfce4-terminal" in win.get_wm_class()[1].lower())
        injector._root.change_property(injector._net_active, Xatom.WINDOW, 32, [window.id])
        injector._d.sync()
        target = injector.snapshot()
        assert target is not None and target.is_terminal
        text = "Проверь Codex CLI, API и /tmp/project.\nВторая строка."
        injector.paste(text, target)
        root.after(1100, root.quit)
        root.mainloop()
        # VTE converts pasted line breaks to CR, including inside bracketed paste.
        payload = text.replace("\n", "\r").encode()
        assert output.read_bytes() == b"\x1b[200~" + payload + b"\x1b[201~"
    finally:
        injector._d.close()
        root.destroy()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
