import os
import select
import shutil
import signal
import subprocess
import time

import pytest
from Xlib import X, display


@pytest.fixture(scope="session")
def x11_display():
    if not shutil.which("Xvfb"):
        pytest.skip("Xvfb is required for X11 integration tests")
    read_fd, write_fd = os.pipe()
    try:
        server = subprocess.Popen(
            ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1024x768x24",
             "-nolisten", "tcp"],
            pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    finally:
        os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 5)[0], "Xvfb did not start in 5 seconds"
        number = os.read(read_fd, 32).decode().strip()
        assert number.isdecimal(), "Xvfb failed to allocate a display"
        yield f":{number}"
    finally:
        os.close(read_fd)
        server.terminate()
        server.communicate(timeout=5)


@pytest.fixture
def x11_window_manager(x11_display, monkeypatch, tmp_path):
    if not all(shutil.which(tool) for tool in ("xfwm4", "dbus-run-session")):
        pytest.skip("xfwm4 and dbus-run-session are required for the terminal test")
    monkeypatch.setenv("DISPLAY", x11_display)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "wm-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    process = subprocess.Popen(
        ["dbus-run-session", "--", "xfwm4", "--compositor=off"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    connection = display.Display(x11_display)
    try:
        root = connection.screen().root
        atom = connection.intern_atom("_NET_SUPPORTING_WM_CHECK")
        deadline = time.monotonic() + 5
        while not root.get_full_property(atom, X.AnyPropertyType):
            assert time.monotonic() < deadline, "window manager did not start"
            time.sleep(0.01)
        yield
    finally:
        connection.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
