"""Сессия диктовки: чтобы вторая фраза пристыковалась к первой.

Сценарий: надиктовал, нажал Enter, текст встал в строку терминала. Вспомнил
ещё что-то — жмёшь клавишу снова, договариваешь, и новое встаёт следом,
а первое остаётся целым.

Сессия привязана к окну: переключился в другое — начинается новая. Демон не
читает содержимое терминала (это недоступно), он помнит только собственные
вставки, поэтому если между диктовками поправить строку руками, стык может
ошибиться на один пробел. Ничего не ломается.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# После этих символов следующая фраза начинается с заглавной.
SENTENCE_END = ".!?…"
# Эти символы уже сами по себе разделители — пробел перед ними не нужен.
NO_SPACE_AFTER = "([{«\"'"


@dataclass
class Session:
    window_id: int
    text: str = ""
    updated_at: float = field(default_factory=time.monotonic)


class SessionStore:
    """Одна активная сессия за раз — диктуют всегда в одно окно."""

    def __init__(self, cfg: dict):
        self.timeout = float(cfg.get("timeout_min", 10)) * 60
        self.smart_join = bool(cfg.get("smart_join", True))
        self._current: Session | None = None

    def previous_text(self, window_id: int) -> str:
        """Что уже вставлено в это окно в рамках живой сессии."""
        session = self._current
        if session is None or session.window_id != window_id:
            return ""
        if time.monotonic() - session.updated_at > self.timeout:
            self._current = None
            return ""
        return session.text

    def join(self, previous: str, addition: str) -> str:
        """Приклеить продолжение к хвосту предыдущей вставки.

        Возвращает только то, что нужно допечатать: предыдущее уже в терминале,
        дублировать его нельзя.
        """
        addition = addition.strip()
        if not addition or not previous or not self.smart_join:
            return addition

        tail = previous[-1]
        if tail.isspace() or tail in NO_SPACE_AFTER:
            prefix = ""
        else:
            prefix = " "

        if tail in SENTENCE_END and addition[:1].islower():
            addition = addition[0].upper() + addition[1:]
        elif tail not in SENTENCE_END and addition[:1].isupper():
            # Продолжение предложения не должно начинаться с заглавной,
            # но аббревиатуры и латиницу не трогаем: API, CI, PostgreSQL.
            head = addition.split(" ", 1)[0]
            if head.isalpha() and head.islower() is False and not head.isupper():
                if head[1:].islower():
                    addition = addition[0].lower() + addition[1:]

        return prefix + addition

    def record(self, window_id: int, inserted: str) -> None:
        """Запомнить вставленное — к нему будет пристыковано следующее."""
        session = self._current
        if session is not None and session.window_id == window_id:
            if time.monotonic() - session.updated_at <= self.timeout:
                session.text += inserted
                session.updated_at = time.monotonic()
                return
        self._current = Session(window_id=window_id, text=inserted.strip())

    def reset(self) -> None:
        self._current = None

    @property
    def active(self) -> bool:
        return self._current is not None
