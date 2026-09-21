"""История диктовок: страховка на случай, если вставка не дошла.

Расшифровка пишется на диск сразу после распознавания — до окна правки и до
вставки. Что бы ни случилось дальше (вставка ушла в никуда, окно закрылось,
демон упал), текст можно достать командой `voicetype history`.

Формат — JSON Lines, по записи на строку, только дозапись: так файл не
побьётся, если процесс умрёт посреди записи. Права 0600 — внутри личные
мысли, а не отладочный мусор.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Держим последние записи, чтобы файл не рос бесконечно.
KEEP_ENTRIES = 500
# Подрезаем не на каждой записи, а когда файл заметно перерос лимит.
TRIM_AT_BYTES = 4 * 1024 * 1024

KIND_LABELS = {
    "transcript": "расшифровка",
    "inserted": "вставлено",
    "failed": "вставка не удалась",
}


class History:
    def __init__(self, path: Path):
        self.path = path
        # Пишут и поток конвейера, и главный поток.
        self._lock = threading.Lock()

    def record(self, kind: str, text: str, **extra) -> None:
        """Дописать запись. Никогда не бросает: история не должна ломать диктовку."""
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            "text": text,
            **{k: v for k, v in extra.items() if v},
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                if self.path.stat().st_size > TRIM_AT_BYTES:
                    self._trim()
        except OSError as exc:
            log.warning("не удалось записать историю %s: %s", self.path, exc)

    def _trim(self) -> None:
        lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines[-KEEP_ENTRIES:])
        os.replace(tmp, self.path)

    def last(self, count: int) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        entries = []
        for line in lines[-count:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # недописанная строка после падения
        return entries


def format_entries(entries: list[dict]) -> str:
    blocks = []
    for e in entries:
        label = KIND_LABELS.get(e.get("kind"), e.get("kind", "?"))
        head = f"── {e.get('time', '?')}  {label}"
        if e.get("window"):
            head += f"  → {e['window']}"
        if e.get("error"):
            head += f"  ({e['error']})"
        text = e.get("text", "")
        head += f"  [{len(text)} симв.]"
        blocks.append(f"{head}\n{text}")
    return "\n\n".join(blocks)
