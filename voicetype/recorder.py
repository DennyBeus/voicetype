"""Запись с микрофона в Opus, целиком в памяти.

pw-record отдаёт сырой PCM в pipe, ffmpeg сжимает его в ogg/opus. Оба уже
есть в системе, так что записи не нужен ни один пакет из pip или apt.

Opus вместо WAV — не про экономию диска (на диск мы ничего не пишем), а про
время выгрузки: минута речи это ~2 МБ в WAV против ~180 КБ в Opus, и на
медленном канале эта разница заметна в общей задержке.
"""

from __future__ import annotations

import logging
import subprocess
import threading

log = logging.getLogger(__name__)


class RecorderError(RuntimeError):
    pass


def list_sources() -> list[str]:
    """Имена источников записи, без monitor-петель."""
    try:
        out = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            names.append(parts[1])
    return names


def check_source(source: str) -> str | None:
    """Проверить источник до записи. Возвращает текст проблемы или None.

    pw-record не ругается на несуществующий --target, а молча берёт источник
    по умолчанию. Без этой проверки опечатка в config.toml выглядела бы как
    «микрофон почему-то пишет тишину».
    """
    names = list_sources()
    if not names:
        return "PipeWire не отдал список источников — проверь, что звук вообще работает"

    real = [n for n in names if not n.endswith(".monitor")]
    if source in ("", "auto"):
        if not real:
            return ("настоящих микрофонов нет, только monitor-петли — "
                    "подключи гарнитуру (scripts/check_mic.sh --bt) или USB-микрофон")
        return None

    if source in names:
        return None
    return (
        f"источник {source!r} из config.toml не найден.\n"
        f"доступны: {', '.join(real) or '(нет микрофонов)'}\n"
        f"pw-record в таком случае молча пишет с устройства по умолчанию"
    )


class Recorder:
    """Одна запись за раз. start() не блокирует, stop() отдаёт байты Opus."""

    def __init__(self, cfg: dict):
        self.source = cfg.get("source", "auto")
        self.rate = int(cfg.get("sample_rate", 16000))
        self.max_seconds = int(cfg.get("max_seconds", 300))
        self.bitrate = cfg.get("opus_bitrate", "24k")

        self._rec: subprocess.Popen | None = None
        self._enc: subprocess.Popen | None = None
        self._chunks: list[bytes] = []
        self._pump: threading.Thread | None = None
        self._limit: threading.Timer | None = None
        self._on_limit = None

    @property
    def active(self) -> bool:
        return self._rec is not None

    def start(self, on_limit=None) -> None:
        if self.active:
            return
        self._on_limit = on_limit
        self._chunks = []

        rec_cmd = ["pw-record", "--rate", str(self.rate), "--channels", "1",
                   "--format", "s16"]
        if self.source and self.source != "auto":
            rec_cmd += ["--target", self.source]
        rec_cmd += ["-"]

        enc_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(self.rate), "-ac", "1", "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", self.bitrate, "-application", "voip",
            "-f", "ogg", "pipe:1",
        ]

        try:
            self._rec = subprocess.Popen(
                rec_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            self._enc = subprocess.Popen(
                enc_cmd, stdin=self._rec.stdout,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            self._teardown()
            raise RecorderError(f"не найдена команда: {exc.filename}") from exc

        # Даём ffmpeg владеть своим концом трубы, иначе он не увидит EOF.
        self._rec.stdout.close()

        # Поток читает по своей ссылке на трубу: stop() обнуляет self._enc,
        # и обращение через атрибут ловило бы гонку на каждой остановке.
        self._pump = threading.Thread(
            target=self._drain, args=(self._enc.stdout,), daemon=True
        )
        self._pump.start()

        self._limit = threading.Timer(self.max_seconds, self._hit_limit)
        self._limit.daemon = True
        self._limit.start()

    def _drain(self, pipe) -> None:
        try:
            for block in iter(lambda: pipe.read(8192), b""):
                self._chunks.append(block)
        except ValueError:
            pass  # труба закрыта из-под нас при аварийной остановке

    def _hit_limit(self) -> None:
        log.warning("достигнут предел записи %s с — останавливаю", self.max_seconds)
        if self._on_limit:
            self._on_limit()

    def stop(self) -> bytes:
        """Остановить запись и вернуть готовый ogg/opus."""
        if not self.active:
            return b""

        if self._limit:
            self._limit.cancel()
            self._limit = None

        rec, enc = self._rec, self._enc
        self._rec = self._enc = None

        rec.terminate()
        try:
            rec.wait(timeout=2)
        except subprocess.TimeoutExpired:
            rec.kill()

        try:
            enc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            enc.kill()

        if self._pump:
            self._pump.join(timeout=3)
            self._pump = None

        audio = b"".join(self._chunks)
        self._chunks = []

        if not audio:
            stderr = (rec.stderr.read() or b"").decode(errors="replace").strip()
            hint = stderr.splitlines()[-1] if stderr else "источник не отдал данных"
            raise RecorderError(
                f"запись пустая: {hint}\n"
                f"проверь источник [audio].source = {self.source!r} "
                f"(pactl list short sources) — возможно, наушники отключились"
            )
        log.info("записано %.1f КБ opus", len(audio) / 1024)
        return audio

    def abort(self) -> None:
        """Бросить запись, не разбирая результат."""
        if not self.active:
            return
        try:
            self.stop()
        except RecorderError:
            pass

    def _teardown(self) -> None:
        for proc in (self._enc, self._rec):
            if proc:
                proc.kill()
        self._rec = self._enc = None
