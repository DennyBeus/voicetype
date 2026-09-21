"""Демон voicetype: хоткей → запись → распознавание → окно правки → вставка.

Tkinter требует, чтобы всё, что трогает виджеты, шло из главного потока,
поэтому раскладка такая:

  главный поток   — mainloop Tk, окно правки, буфер обмена;
  поток RECORD    — слушает клавишу (внутри Xlib);
  поток конвейера — запись и сетевые запросы, чтобы не морозить интерфейс.

Из фоновых потоков в Tk возвращаемся только через очередь `_ui`, которую
разбирает таймер главного потока. Соблазнительный `root.after()` для этого
не годится: Tcl не потокобезопасен, и вызов из чужого потока не падает с
ошибкой, а молча теряется — окно просто не появляется, и в логах пусто.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

from . import config
from .cleanup import Cleaner
from .history import History, format_entries
from .hotkey import HotkeyError, HotkeyListener
from .inject import Injector
from . import recorder as recorder_mod
from .recorder import Recorder, RecorderError
from .session import SessionStore
from .stt import SttError, Transcriber
from . import ui as ui_mod
from .ui import Beeper, ReviewWindow

log = logging.getLogger("voicetype")

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / ".state"
STATUS_FILE = STATE_DIR / "status.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"


class Daemon:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self.started_at = time.time()

        self.root = tk.Tk()
        self.root.withdraw()

        self.beeper = Beeper(bool(cfg.ui.get("beep", True)))
        self.window = ReviewWindow(self.root, cfg.ui)
        self.injector = Injector(cfg.inject, self.root)
        self.sessions = SessionStore(cfg.session)
        self.history = History(HISTORY_FILE)
        self.recorder = Recorder(cfg.audio)
        self.stt = Transcriber(cfg.stt, cfg.openai_key, cfg.keywords)
        self.cleaner = Cleaner(
            cfg.cleanup, cfg.deepseek_key, cfg.cleanup_enabled, cfg.cleanup_off_reason
        )

        self.hotkeys = HotkeyListener(cfg.hotkey, self._on_start, self._on_stop)

        self._target = None
        self._busy = threading.Lock()
        # Единственный канал из фоновых потоков в Tk.
        self._ui: queue.Queue = queue.Queue()

    # ── жизненный цикл ──────────────────────────────────────────────────
    def run(self) -> int:
        self._write_status("running")
        self.hotkeys.start()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self._post(self.shutdown))
        # Он же будит Tk, чтобы сигналы обрабатывались без внешних событий.
        self._tick()

        if ui_mod.fonts_are_degraded():
            log.warning(
                "Tk не видит системные шрифты — окно будет плохо читаться. "
                "Лечится: sudo apt install python3-tk && bash install.sh"
            )

        log.info(
            "готов. клавиша %s, микрофон %s, вычитка %s",
            self.cfg.hotkey.get("key"),
            self.cfg.audio.get("source"),
            "включена" if self.cleaner.enabled else f"выключена ({self.cleaner.off_reason})",
        )
        self.beeper.play("stop")
        try:
            self.root.mainloop()
        finally:
            self._cleanup()
        return 0

    def _post(self, fn, *args) -> None:
        """Попросить главный поток выполнить fn(*args). Безопасно отовсюду."""
        self._ui.put((fn, args))

    def _tick(self) -> None:
        """Разбор очереди в главном потоке — единственное место, где живёт Tk."""
        while True:
            try:
                fn, args = self._ui.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 — один сбой не должен ронять цикл
                log.exception("обработчик из очереди упал")
        self.root.after(50, self._tick)

    def shutdown(self) -> None:
        log.info("останавливаюсь")
        self.root.quit()

    def _cleanup(self) -> None:
        self.recorder.abort()
        try:
            self.hotkeys.stop()
        except Exception:  # noqa: BLE001
            pass
        STATUS_FILE.unlink(missing_ok=True)

    # ── реакция на клавишу (вызывается из потока RECORD) ────────────────
    def _on_start(self) -> None:
        if self.recorder.active:
            return
        # Окно правки уже открыто — дописываем в него, фокус менять не нужно.
        if not self.window.open:
            self._target = self.injector.snapshot()
            log.info("цель: %r", self._target)
        try:
            self.recorder.start(on_limit=lambda: self._post(self._on_stop))
        except RecorderError as exc:
            self._fail(str(exc))
            return
        self.beeper.play("start")

    def _on_stop(self) -> None:
        if not self.recorder.active:
            return
        self.beeper.play("stop")
        try:
            audio = self.recorder.stop()
        except RecorderError as exc:
            self._fail(str(exc))
            return

        if not self._busy.acquire(blocking=False):
            log.warning("предыдущая диктовка ещё обрабатывается — пропускаю")
            return
        threading.Thread(target=self._pipeline, args=(audio,), daemon=True).start()

    # ── конвейер (фоновый поток) ────────────────────────────────────────
    def _pipeline(self, audio: bytes) -> None:
        try:
            appending = self.window.open
            previous = "" if appending else self._previous_text()

            text = self.stt.transcribe(audio, previous)
            if not text:
                self._fail("ничего не распознано — тишина или слишком тихо")
                return
            raw = text
            # До вычитки и окна: дальше может случиться что угодно.
            self.history.record("transcript", raw, window=self._target_name())

            result = self.cleaner.clean(text, previous)
            self._post(
                self._present, result.text, raw, previous, result.note, appending
            )
        except SttError as exc:
            self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 — фоновый поток не должен молчать
            log.exception("конвейер упал")
            self._fail(f"внутренняя ошибка: {exc}")
        finally:
            self._busy.release()

    def _previous_text(self) -> str:
        if self._target is None:
            return ""
        return self.sessions.previous_text(self._target.window_id)

    # ── окно и вставка (главный поток) ──────────────────────────────────
    def _present(self, text: str, raw: str, previous: str,
                 note: str | None, appending: bool) -> None:
        if appending and self.window.open:
            self.window.append_dictation(text)
            return
        self.window.show(
            text, previous=previous, raw=raw, note=note, on_done=self._on_done
        )

    def _on_done(self, value: str | None, reset: bool) -> None:
        if reset:
            self.sessions.reset()
        if value is None:
            log.info("отменено")
            return

        target = self._target
        previous = self._previous_text()
        to_insert = self.sessions.join(previous, value) if previous else value

        try:
            self.injector.paste(to_insert, target)
        except Exception as exc:  # noqa: BLE001
            self.history.record("failed", to_insert, window=self._target_name(),
                                error=str(exc))
            self._fail(f"не удалось вставить текст: {exc}")
            return

        # Итог после правки в окне — он может отличаться от расшифровки.
        self.history.record("inserted", to_insert, window=self._target_name())

        if target is not None:
            self.sessions.record(target.window_id, to_insert)
        log.info("вставлено %s символов", len(to_insert))

    def _target_name(self) -> str:
        return repr(self._target) if self._target is not None else ""

    def _fail(self, message: str) -> None:
        log.error(message)
        self.beeper.play("error")
        self.hotkeys.reset()

    # ── статус для CLI ──────────────────────────────────────────────────
    def _write_status(self, state: str) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        payload = {
            "state": state,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "hotkey": self.cfg.hotkey.get("key"),
            "source": self.cfg.audio.get("source"),
            "stt_model": self.cfg.stt.get("model"),
            "cleanup": "on" if self.cleaner.enabled else f"off ({self.cleaner.off_reason})",
            "keywords": len(self.cfg.keywords),
        }
        STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def preflight(cfg: config.Config) -> list[str]:
    """Проверки, которые лучше провалить на старте, чем посреди диктовки."""
    problems = []
    if not cfg.openai_key:
        problems.append(
            "нет OPENAI_API_KEY — впиши ключ в .env (шаблон: .env.example)"
        )
    if not os.environ.get("DISPLAY"):
        problems.append("нет DISPLAY — демон должен запускаться внутри графической сессии")
    for tool in ("pw-record", "ffmpeg"):
        if not _which(tool):
            problems.append(f"не найдена команда {tool}")

    mic = recorder_mod.check_source(str(cfg.audio.get("source", "auto")))
    if mic:
        problems.append(mic)

    if config.env_file_is_exposed():
        log.warning(".env читаем не только владельцем — поправь: chmod 600 .env")
    return problems


def _which(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def main() -> int:
    parser = argparse.ArgumentParser(prog="voicetype", description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="проверить конфигурацию и выйти")
    parser.add_argument("--history", type=int, metavar="N", nargs="?", const=5,
                        help="показать последние N записей истории диктовок и выйти")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.history is not None:
        entries = History(HISTORY_FILE).last(args.history)
        print(format_entries(entries) if entries else f"история пуста: {HISTORY_FILE}")
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = config.load()
    problems = preflight(cfg)

    if args.check:
        print(f"клавиша:    {cfg.hotkey.get('key')}")
        print(f"микрофон:   {cfg.audio.get('source')}")
        print(f"модель STT: {cfg.stt.get('model')}")
        print(f"вычитка:    "
              f"{'включена' if cfg.cleanup_enabled else f'выключена ({cfg.cleanup_off_reason})'}")
        print(f"терминов:   {len(cfg.keywords)}")
        for problem in problems:
            print(f"ПРОБЛЕМА:   {problem}")
        return 1 if problems else 0

    if problems:
        for problem in problems:
            log.error(problem)
        return 1

    try:
        daemon = Daemon(cfg)
    except (HotkeyError, SttError) as exc:
        log.error("%s", exc)
        return 1
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
