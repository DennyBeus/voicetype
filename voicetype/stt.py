"""Распознавание речи через OpenAI /v1/audio/transcriptions.

Модель по умолчанию — gpt-transcribe. Для нашей задачи важны три параметра
контекста, которых не было у whisper-1:

  keywords   термины пишутся как надо (kubectl, а не «кубэ контрол»);
  languages  список языков вместо одного — русская речь с англицизмами
             перестаёт транслитерироваться кириллицей;
  prompt     свободное описание ситуации.

Старые модели этих параметров не знают, поэтому на 400 с жалобой на
неизвестное поле запрос повторяется без них — так конфиг можно откатить
на whisper-1, ничего не меняя в коде.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

URL = "https://api.openai.com/v1/audio/transcriptions"
CONTEXT_FIELDS = ("keywords", "languages", "prompt")


class SttError(RuntimeError):
    pass


class Transcriber:
    def __init__(self, cfg: dict, api_key: str, keywords: list[str]):
        if not api_key:
            raise SttError(
                "нет OPENAI_API_KEY — впиши ключ в .env в корне проекта\n"
                "(шаблон лежит рядом: .env.example)"
            )
        self.model = cfg.get("model", "gpt-transcribe")
        self.languages = list(cfg.get("languages", ["ru", "en"]))
        self.prompt = cfg.get("prompt", "")
        self.timeout = float(cfg.get("timeout_s", 30))
        self.retries = int(cfg.get("retries", 2))
        self.keywords = keywords
        self._key = api_key
        self._drop_context = False  # взводится, если модель их не понимает

    def transcribe(self, audio: bytes, previous: str = "") -> str:
        """Отдать Opus-аудио и получить текст.

        `previous` — предыдущий фрагмент этой же сессии диктовки; он идёт
        в prompt, чтобы продолжение стыковалось по смыслу и пунктуации.
        """
        if not audio:
            raise SttError("нечего распознавать: аудио пустое")

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._request(audio, previous)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 400 and not self._drop_context:
                    body = exc.response.text
                    if any(f in body for f in CONTEXT_FIELDS):
                        log.warning(
                            "модель %s не принимает параметры контекста — "
                            "повторяю без них", self.model,
                        )
                        self._drop_context = True
                        continue
                if status in (429, 500, 502, 503, 504) and attempt < self.retries:
                    pause = 2 ** attempt
                    log.warning("OpenAI ответил %s, повтор через %s с", status, pause)
                    time.sleep(pause)
                    last_error = exc
                    continue
                raise SttError(self._explain(exc)) from exc
            except httpx.RequestError as exc:
                if attempt < self.retries:
                    time.sleep(2 ** attempt)
                    last_error = exc
                    continue
                raise SttError(f"нет связи с OpenAI: {exc}") from exc

        raise SttError(f"OpenAI недоступен: {last_error}")

    def _request(self, audio: bytes, previous: str) -> str:
        data: dict[str, object] = {"model": self.model}

        if not self._drop_context:
            prompt = self.prompt
            if previous:
                prompt = (
                    f"{prompt}\n\nПредыдущая часть этой же диктовки: "
                    f"«{previous[-500:]}». Дальше идёт её продолжение."
                ).strip()
            if prompt:
                data["prompt"] = prompt
            if self.languages:
                data["languages"] = self.languages
            if self.keywords:
                data["keywords"] = self.keywords

        files = {"file": ("speech.ogg", audio, "audio/ogg")}
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                URL,
                headers={"Authorization": f"Bearer {self._key}"},
                data=data,
                files=files,
            )
        resp.raise_for_status()
        text = (resp.json().get("text") or "").strip()
        log.info("распознано %s символов", len(text))
        return text

    @staticmethod
    def _explain(exc: httpx.HTTPStatusError) -> str:
        status = exc.response.status_code
        detail = exc.response.text[:300]
        if status == 401:
            return "OpenAI отклонил ключ (401) — проверь OPENAI_API_KEY в .env"
        if status == 429:
            return "OpenAI: превышен лимит запросов или кончились кредиты (429)"
        if status == 413:
            return "запись слишком длинная для одного запроса (413)"
        return f"OpenAI вернул {status}: {detail}"


def _cli() -> int:
    """python -m voicetype.stt --file запись.ogg"""
    import argparse
    import pathlib

    from . import config

    parser = argparse.ArgumentParser(description="Распознать файл через OpenAI")
    parser.add_argument("--file", required=True, type=pathlib.Path)
    parser.add_argument("--previous", default="", help="контекст предыдущей диктовки")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = config.load()
    engine = Transcriber(cfg.stt, cfg.openai_key, cfg.keywords)
    print(engine.transcribe(args.file.read_bytes(), args.previous))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
