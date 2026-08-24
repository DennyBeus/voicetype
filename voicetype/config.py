"""Загрузка настроек: config.toml, .env и keywords.txt.

Всё лежит в корне проекта. Личные файлы (config.toml, keywords.txt, .env)
игнорируются git; рядом с каждым лежит закоммиченный *.example как эталон
и как запасной вариант, если личного файла ещё нет.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pick(name: str, example: str) -> Path:
    """Личный файл, если он есть, иначе эталон."""
    personal = ROOT / name
    return personal if personal.exists() else ROOT / example


def _deep_merge(base: dict, override: dict) -> dict:
    """Значения из override поверх base, рекурсивно по секциям."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_env() -> dict[str, str]:
    """Разбор .env. Отсутствие файла — не ошибка.

    Реальное окружение имеет приоритет: так ключ можно подсунуть на один запуск,
    не трогая файл.
    """
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def env_file_is_exposed() -> bool:
    """True, если .env читаем кем-то кроме владельца."""
    path = ROOT / ".env"
    if not path.exists():
        return False
    return bool(path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH))


def load_keywords() -> list[str]:
    """Термины для параметра keywords у модели распознавания."""
    path = _pick("keywords.txt", "keywords.example.txt")
    if not path.exists():
        return []
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line)
    return words


@dataclass
class Config:
    audio: dict
    hotkey: dict
    stt: dict
    cleanup: dict
    session: dict
    inject: dict
    ui: dict

    openai_key: str = ""
    deepseek_key: str = ""
    keywords: list[str] = field(default_factory=list)

    @property
    def cleanup_enabled(self) -> bool:
        """DeepSeek подключается, только если он и разрешён, и есть ключ.

        Отсутствие ключа — штатная конфигурация, а не ошибка: приложение
        работает на одном OpenAI.
        """
        return bool(self.cleanup.get("enabled", True)) and bool(self.deepseek_key)

    @property
    def cleanup_off_reason(self) -> str | None:
        if self.cleanup_enabled:
            return None
        if not self.cleanup.get("enabled", True):
            return "выключен в config.toml"
        return "нет ключа"


def load() -> Config:
    defaults = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    personal_path = ROOT / "config.toml"
    if personal_path.exists():
        data = _deep_merge(
            defaults, tomllib.loads(personal_path.read_text(encoding="utf-8"))
        )
    else:
        data = defaults

    env = load_env()
    return Config(
        audio=data["audio"],
        hotkey=data["hotkey"],
        stt=data["stt"],
        cleanup=data["cleanup"],
        session=data["session"],
        inject=data["inject"],
        ui=data["ui"],
        openai_key=env.get("OPENAI_API_KEY", ""),
        deepseek_key=env.get("DEEPSEEK_API_KEY", ""),
        keywords=load_keywords(),
    )
