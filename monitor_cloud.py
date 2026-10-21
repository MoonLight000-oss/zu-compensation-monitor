
"""Cloud monitor for official Kubcenter compensation announcements."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# Telegram secrets are configured in GitHub Settings.
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [
    os.getenv("TELEGRAM_CHAT_ID_MOM", "").strip(),
    os.getenv("TELEGRAM_CHAT_ID_DAD", "").strip(),
]
CHAT_IDS = [chat_id for chat_id in CHAT_IDS if chat_id]

SECTION_URL = (
    "https://xn--90ainopldz.xn--p1ai/"
    "%D0%BA%D0%BE%D0%BC%D0%BF%D0%B5%D0%BD%D1%81"
    "%D0%B0%D1%86%D0%B8%D0%BE%D0%BD%D0%BD%D1%8B%D0%B5-"
    "%D0%B2%D1%8B%D0%BF%D0%BB%D0%B0%D1%82%D1%8B/"
)
APPLICATION_URL = "https://gpn.krasnodar.ru/"
STATE_PATH = Path("monitor_state.json")

USER_AGENT = (
    "Mozilla/5.0 (compatible; ZU-Compensation-Monitor/3.0)"
)
REQUEST_TIMEOUT = (10, 20)
FETCH_ATTEMPTS = 4
RETRY_DELAYS = (5, 10, 15)
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}

READER_ATTEMPTS = 2

# Known old publications are the baseline for the first run.
KNOWN_BASELINE_ITEMS = [
    {
        "title": "Приказ о продлении приема 65 от 06.02.2026.",
        "url": (
            "https://xn--90ainopldz.xn--p1ai/"
            "компенсационные-выплаты/"
            "65-от-06-02-2026-о-продлении-приема-до-28-02-2026/"
        ),
    },
    {
        "title": "Приказ о приёме заявлений 21 от 15.01.2026.",
        "url": (
            "https://xn--90ainopldz.xn--p1ai/"
            "компенсационные-выплаты/"
            "приказ-о-приёме-заявлений-21-от-15-01-2026/"
        ),
    },
]

INTAKE_WORDS = (
    "прием", "приема", "приеме", "приемом", "приему",
    "продлен", "открыт",
)
CONTEXT_WORDS = (
    "заявлен", "приказ", "компенсац", "выплат",
    "земельн", "участии", "многодет", "подач",
    "продлен", "открыт",
)


def log(message: str) -> None:
    """Print immediately so messages appear in Actions logs."""
    print(message, flush=True)


def normalize(value: str) -> str:
    value = unquote(value or "")
    value = value.replace("ё", "е").replace("Ё", "Е")
    value = value.replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value.casefold()


def relevant_announcement(title: str) -> bool:
    normalized = normalize(title)
    return (
        any(word in normalized for word in INTAKE_WORDS)
        and any(word in normalized for word in CONTEXT_WORDS)
    )


def item_key(title: str, url: str) -> str:
    path = unquote(urlparse(url).path or "/")
    value = f"{normalize(title)}|{normalize(path)}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_item(
    title: str,
    url: str,
    kind: str = "publication",
) -> dict[str, str]:
    title = re.sub(r"\s+", " ", title).strip()
    url = urljoin(SECTION_URL, url)
    return {
        "key": item_key(title, url),
        "title": title,
        "url": url,
        "kind": kind,
    }


def baseline_state() -> dict:
    return {
        "initialized": True,
        "current_items": [
            canonical_item(
                item["title"], item["url"], "known archived notice"
            )
            for item in KNOWN_BASELINE_ITEMS
        ],
    }


def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if (
            data.get("initialized") is True
            and isinstance(data.get("current_items"), list)
        ):
            return data
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass

    return baseline_state()


def save_state(items: list[dict[str, str]]) -> None:
    normalized_items = sorted(
        [
            {
                "key": item["key"],
                "title": item["title"],
                "url": item["url"],
                "kind": item["kind"],
            }
            for item in items
        ],
        key=lambda item: (
            item["title"].casefold(), item["url"]
        ),
    )

    try:
        existing = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}

    old_items = existing.get("current_items", [])
    normalized_old = []

    if isinstance(old_items, list):
        for item in old_items:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            url = item.get("url")
            if not title or not url:
                continue
            normalized_old.append({
                "key": item.get("key") or item_key(title, url),
                "title": title,
                "url": url,
                "kind": item.get("kind", "publication"),
            })

    normalized_old.sort(
        key=lambda item: (
            item["title"].casefold(), item["url"]
        )
    )

    if (
        existing.get("initialized") is True
        and normalized_old == normalized_items
    ):
        log("[СОСТОЯНИЕ] Публикации не изменились.")
        return

    data = {
        "initialized": True,
        "current_items": normalized_items,
        "updated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds"),
    }

    STATE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log("[СОСТОЯНИЕ] Состояние сохранено.")


def get_official_page_response() -> requests.Response:
    """Try Kubcenter directly, then try the public Jina Reader proxy."""

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    }

    last_direct_error = None

    # First try the official site directly.
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            log(
                f"[СЕТЬ] Прямое подключение "
                f"{attempt}/{FETCH_ATTEMPTS}..."
            )

            response = requests.get(
                SECTION_URL,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()

            log("[СЕТЬ] Официальный сайт доступен напрямую.")
            return response

        except requests.RequestException as exc:
            last_direct_error = exc
            log(
                f"[СЕТЬ] Прямая попытка {attempt} не удалась: "
                f"{type(exc).__name__}."
            )

            status = (
                exc.response.status_code
                if isinstance(exc, requests.HTTPError)
                and exc.response is not None
                else None
            )

            can_retry = (
                attempt < FETCH_ATTEMPTS
                and (
                    not isinstance(exc, requests.HTTPError)
                    or status in RETRYABLE_HTTP_STATUSES
                )
            )

            if can_retry:
                delay = RETRY_DELAYS[attempt - 1]
                log(f"[СЕТЬ] Повтор через {delay} секунд.")
                time.sleep(delay)
            else:
                break

    log("[СЕТЬ] Прямой доступ не сработал. Пробую резервный источник.")

    reader_url = "https://r.jina.ai/" + SECTION_URL
    reader_headers = {
        "User-Agent": USER_AGENT,
        "X-Respond-With": "html",
        "X-No-Cache": "true",
    }

    last_reader_error = None

    for attempt in range(1, READER_ATTEMPTS + 1):
        try:
            log(
                f"[СЕТЬ] Jina Reader: "
                f"попытка {attempt}/{READER_ATTEMPTS}..."
            )

            response = requests.get(
                reader_url,
                headers=reader_headers,
                timeout=(10, 30),
            )
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "html.parser")
            page_text = normalize(soup.get_text(" ", strip=True))

            if "компенсационные выплаты" not in page_text:
                raise RuntimeError(
                    "Резервный ответ не похож на нужную страницу."
                )

            if not soup.find("a", href=True):
                raise RuntimeError(
                    "В резервном ответе не найдены ссылки."
                )

            log("[СЕТЬ] Страница получена через Jina Reader.")
            return response

        except Exception as exc:
            last_reader_error = exc
            log(
                f"[СЕТЬ] Ошибка Jina Reader: "
                f"{type(exc).__name__}: {exc}"
            )

            if attempt < READER_ATTEMPTS:
                time.sleep(5)

    raise RuntimeError(
        "Не удалось загрузить страницу ни напрямую, ни через Jina Reader. "
        f"Прямая ошибка: {type(last_direct_error).__name__}; "
        f"ошибка резерва: {type(last_reader_error).__name__}."
    ) from last_reader_error


def fetch_relevant_items() -> list[dict[str, str]]:
    """Fetch the compensation index and detect relevant linked notices."""

    log("[МОНИТОР] Начинаю проверку официального раздела.")
    response = get_official_page_response()
    soup = BeautifulSoup(response.content, "html.parser")

    page_text = normalize(soup.get_text(" ", strip=True))
    if "компенсационные выплаты" not in page_text:
        raise RuntimeError(
            "Ответ не похож на раздел «Компенсационные выплаты»."
        )

    found: dict[str, dict[str, str]] = {}

    # Linked notices, including titles beginning with "Приказ".
    for anchor in soup.find_all("a", href=True):
        title = re.sub(
            r"\s+", " ", anchor.get_text(" ", strip=True)
        ).strip()

        if not title or not relevant_announcement(title):
            continue

        href = urljoin(SECTION_URL, anchor["href"].strip())
        if urlparse(href).scheme not in {"http", "https"}:
            continue

        item = canonical_item(title, href, "ссылка на публикацию")
        found[item["key"]] = item

    # Headings that contain a relevant announcement.
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        title = re.sub(
            r"\s+", " ", heading.get_text(" ", strip=True)
        ).strip()

        if not title or not relevant_announcement(title):
            continue

        link = heading.find("a", href=True)
        href = (
            urljoin(SECTION_URL, link["href"].strip())
            if link
            else SECTION_URL
        )

        item = canonical_item(title, href, "заголовок страницы")
        found[item["key"]] = item

    log(
        f"[МОНИТОР] Страница обработана. "
        f"Релевантных публикаций: {len(found)}."
    )

    for item in sorted(found.values(), key=lambda x: x["title"].casefold()):
        log(f"  - {item['title']} | {item['url']}")

    return list(found.values())


def send_telegram(message: str) -> bool:
    if not BOT_TOKEN:
        log("[ОШИБКА] Не задан secret TELEGRAM_BOT_TOKEN.")
        return False

    if len(CHAT_IDS) != 2:
        log(
            "[ОШИБКА] Не заданы TELEGRAM_CHAT_ID_MOM "
            "и TELEGRAM_CHAT_ID_DAD."
        )
        return False

    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    all_ok = True

    for index, chat_id in enumerate(CHAT_IDS, start=1):
        try:
            response = requests.post(
                api_url,
                data={
                    "chat_id": chat_id,
                    "text": message,
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            response.raise_for_status()

            if response.json().get("ok") is not True:
                all_ok = False
                log(f"[TELEGRAM] Не подтверждена отправка #{index}.")
            else:
                log(f"[TELEGRAM] Сообщение отправлено получателю #{index}.")

        except Exception as exc:
            all_ok = False
            log(
                f"[TELEGRAM] Ошибка отправки #{index}: "
                f"{type(exc).__name__}."
            )

    return all_ok


def validate_secrets() -> bool:
    if not BOT_TOKEN:
        log("[ОШИБКА] Отсутствует TELEGRAM_BOT_TOKEN.")
        return False

    if len(CHAT_IDS) != 2:
        log("[ОШИБКА] Не заданы оба Telegram chat ID.")
        return False

    return True


def send_test() -> int:
    if not validate_secrets():
        return 2

    message = (
        "✅ Тест облачного монитора компенсации ЗУ.\n\n"
        "Связь с Telegram настроена.\n"
        "Это тестовое сообщение и оно не подтверждает, "
        "что приём заявлений открыт.\n\n"
        f"Раздел: {SECTION_URL}\n"
        f"Портал подачи: {APPLICATION_URL}"
    )

    return 0 if send_telegram(message) else 1


def make_alert(items: list[dict[str, str]]) -> str:
    stamp = datetime.now().astimezone().strftime(
        "%d.%m.%Y %H:%M:%S %Z"
    )

    details = "\n".join(
        f"• {item['title']}\n{item['url']}"
        for item in items[:8]
    )

    return (
        "🚨 НОВАЯ ПУБЛИКАЦИЯ О ПРИЁМЕ КОМПЕНСАЦИИ ЗУ\n\n"
        f"Время обнаружения: {stamp}\n\n"
        f"Что найдено:\n{details}\n\n"
        f"Раздел Кубцентра: {SECTION_URL}\n"
        f"Портал подачи заявления: {APPLICATION_URL}\n\n"
        "Проверьте приказ и форму вручную. "
        "Публикация сама по себе не подтверждает, "
        "что приём открыт в настоящий момент."
    )


def run_monitor() -> int:
    log("[МОНИТОР] Запуск проверки.")

    if not validate_secrets():
        return 2

    try:
        current_items = fetch_relevant_items()
    except Exception as exc:
        log(
            f"[ОШИБКА] Проверка не удалась: "
            f"{type(exc).__name__}: {exc}"
        )
        return 1

    state = load_state()
    previous_items = state.get("current_items", [])

    previous_by_key = {
        item.get("key") or item_key(item.get("title", ""), item.get("url", "")): item
        for item in previous_items
        if isinstance(item, dict) and item.get("title") and item.get("url")
    }
    current_by_key = {item["key"]: item for item in current_items}

    new_keys = set(current_by_key) - set(previous_by_key)
    new_items = [
        current_by_key[key] for key in sorted(new_keys)
    ]

    if new_items:
        log(
            f"[МОНИТОР] Обнаружено новых публикаций: "
            f"{len(new_items)}. Отправляю уведомления."
        )

        if not send_telegram(make_alert(new_items)):
            log(
                "[ОШИБКА] Уведомления доставлены не всем получателям. "
                "Состояние не сохраняю."
            )
            return 1

        log("[МОНИТОР] Уведомления успешно отправлены обоим получателям.")
    else:
        log("[МОНИТОР] Новых релевантных публикаций нет.")

    save_state(current_items)
    log("[МОНИТОР] Проверка завершена успешно.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Монитор официальных объявлений Кубцентра."
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Отправить тестовое сообщение в Telegram.",
    )
    args = parser.parse_args()

    if args.test:
        return send_test()

    return run_monitor()


if __name__ == "__main__":
    sys.exit(main())
