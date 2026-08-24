"""Вычитка транскрипта через DeepSeek — необязательный шаг конвейера.

Термины уже разобраны на этапе распознавания (параметр keywords), поэтому
здесь остаётся только стиль: слова-паразиты, оговорки, самоповторы,
пунктуация.

Шага может не быть вовсе. Нет ключа — не ошибка и не предупреждение,
а штатный режим: приложение работает на одном OpenAI. Уже включённый шаг
не имеет права ломать диктовку, поэтому любая ошибка сети, таймаут или
отказ по ключу возвращают исходный текст (fail-open) с пометкой о причине.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты редактируешь расшифровку устной речи русскоязычного разработчика.

Что делать:
- убрать слова-паразиты («ну», «эээ», «как бы», «short», «типа» в роли заполнителя),
  оговорки и самоповторы;
- расставить знаки препинания и заглавные буквы;
- починить очевидные ошибки согласования, возникшие от устной речи.

Чего НЕ делать:
- не менять смысл, не сокращать и не пересказывать;
- ничего не добавлять от себя, никаких комментариев и пояснений;
- не переводить между языками;
- технические термины, имена команд, пути и названия оставить ровно как есть.

Верни только исправленный текст, без кавычек и обрамления."""


@dataclass
class CleanupResult:
    text: str
    changed: bool
    note: str | None = None  # почему вычитки не случилось


class Cleaner:
    """Может быть выключен. Тогда просто пропускает текст насквозь."""

    def __init__(self, cfg: dict, api_key: str, enabled: bool, off_reason: str | None):
        self.enabled = enabled
        self.off_reason = off_reason
        self.model = cfg.get("model", "deepseek-chat")
        self.base_url = cfg.get("base_url", "https://api.deepseek.com").rstrip("/")
        self.timeout = float(cfg.get("timeout_s", 6))
        self._key = api_key

    def clean(self, text: str, previous: str = "") -> CleanupResult:
        if not self.enabled:
            return CleanupResult(text, changed=False)
        if not text.strip():
            return CleanupResult(text, changed=False)

        user = text
        if previous:
            user = (
                f"Предыдущая часть диктовки (уже вставлена, её редактировать не нужно): "
                f"«{previous[-500:]}»\n\n"
                f"Отредактируй только продолжение:\n{text}"
            )

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={
                        "model": self.model,
                        "temperature": 0,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user},
                        ],
                    },
                )
            resp.raise_for_status()
            edited = resp.json()["choices"][0]["message"]["content"].strip()
        except httpx.TimeoutException:
            log.warning("DeepSeek не ответил за %s с — отдаю текст как есть", self.timeout)
            return CleanupResult(text, changed=False, note="DeepSeek не успел")
        except httpx.HTTPStatusError as exc:
            note = self._explain(exc)
            log.warning("DeepSeek: %s — отдаю текст как есть", note)
            return CleanupResult(text, changed=False, note=note)
        except (httpx.RequestError, KeyError, IndexError, ValueError) as exc:
            log.warning("DeepSeek недоступен (%s) — отдаю текст как есть", exc)
            return CleanupResult(text, changed=False, note="DeepSeek недоступен")

        if not edited:
            return CleanupResult(text, changed=False, note="DeepSeek вернул пустоту")
        return CleanupResult(edited, changed=edited != text)

    @staticmethod
    def _explain(exc: httpx.HTTPStatusError) -> str:
        status = exc.response.status_code
        if status == 401:
            return "DeepSeek отклонил ключ"
        if status == 402:
            return "на счёте DeepSeek нет средств"
        if status == 429:
            return "DeepSeek: лимит запросов"
        return f"DeepSeek вернул {status}"


def _cli() -> int:
    """python -m voicetype.cleanup --text "ну это ээ надо бы заребейзить" """
    import argparse

    from . import config

    parser = argparse.ArgumentParser(description="Прогнать текст через вычитку")
    parser.add_argument("--text", required=True)
    parser.add_argument("--previous", default="")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = config.load()
    cleaner = Cleaner(
        cfg.cleanup, cfg.deepseek_key, cfg.cleanup_enabled, cfg.cleanup_off_reason
    )
    if not cleaner.enabled:
        print(f"[вычитка выключена: {cleaner.off_reason}]")
    result = cleaner.clean(args.text, args.previous)
    if result.note:
        print(f"[{result.note}]")
    print(result.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
