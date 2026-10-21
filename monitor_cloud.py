"""Monitor official Kubcenter compensation announcements and notify Telegram.

This checks the official "Компенсационные выплаты" index once per GitHub Actions
scheduled run. It tracks relevant announcement links/headings, rather than only
looking for a heading beginning with the literal phrase "Прием заявлений".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [
    os.getenv("TELEGRAM_CHAT_ID_MOM", "").strip(),
    os.getenv("TELEGRAM_CHAT_ID_DAD", "").strip(),
]
CHAT_IDS = [value for value in CHAT_IDS if value]

SECTION_NAME = "Компенсационные выплаты — официальный сайт Кубцентра"
SECTION_URL = "https://xn--90ainopldz.xn--p1ai/%D0%BA%D0%BE%D0%BC%D0%BF%D0%B5%D0%BD%D1%81%D0%B0%D1%86%D0%B8%D0%BE%D0%BD%D0%BD%D1%8B%D0%B5-%D0%B2%D1%8B%D0%BF%D0%BB%D0%B0%D1%82%D1%8B/"
APPLICATION_URL = "https://gpn.krasnodar.ru/"
STATE_PATH = Path("monitor_state.json")
REQUEST_TIMEOUT = (10, 25)
USER_AGENT = (
    "Mozilla/5.0 (compatible; ZU-Compensation-Notice-Monitor/2.0; "
    "+https://github.com/; respectful monitoring)"
)

# Seeded from the official Kubcenter page checked on 21 October 2026. These two
# links are already-known archived notices, so the first run won't alert for them.
# If the site gets another relevant notice before the first run, it will be new
# relative to this baseline and will trigger an alert.
KNOWN_BASELINE_ITEMS = [
    {
        "title": "Приказ о продлении приема 65 от 06.02.2026.",
        "url": "https://xn--90ainopldz.xn--p1ai/компенсационные-выплаты/65-от-06-02-2026-о-продлении-приема-до-28-02-2026/",
    },
    {
        "title": "Приказ о приёме заявлений 21 от 15.01.2026.",
        "url": "https://xn--90ainopldz.xn--p1ai/компенсационные-выплаты/приказ-о-приёме-заявлений-21-от-15-01-2026/",
    },
]

# The compensation index is already scoped to this program. Match relevant
# announcements wherever the wording appears, including titles like
# "Приказ о продлении приема ..." and "О приеме заявлений ...".
INTAKE_WORDS = ("прием", "приема", "приеме", "приемом", "приему", "продлен", "открыт")
CONTEXT_WORDS = (
    "заявлен", "приказ", "компенсац", "выплат", "земельн", "участии", "многодет",
    "подач", "продлен", "открыт",
)


def normalize(value: str) -> str:
    value = unquote(value or "")
    value = value.replace("ё", "е").replace("Ё", "Е").replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value).strip().casefold()
    return value


def relevant_announcement(text: str) -> bool:
    normalized = normalize(text)
    return any(word in normalized for word in INTAKE_WORDS) and any(
        word in normalized for word in CONTEXT_WORDS
    )


def item_key(title: str, url: str) -> str:
    path = unquote(urlparse(url).path or "/")
    material = f"{normalize(title)}|{normalize(path)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def canonical_item(title: str, url: str, kind: str = "publication") -> dict[str, str]:
    title = re.sub(r"\s+", " ", title).strip()
    url = urljoin(SECTION_URL, url)
    return {
        "key": item_key(title, url),
        "title": title,
        "url": url,
        "kind": kind,
    }


def baseline_state() -> dict[str, Any]:
    return {
        "initialized": True,
        "current_items": [
            canonical_item(item["title"], item["url"], "known archived notice")
            for item in KNOWN_BASELINE_ITEMS
        ],
        "updated_at_utc": None,
    }


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if data.get("initialized") is True and isinstance(data.get("current_items"), list):
            return data
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    # Also migrates the older state format {"present": false} from v1.
    return baseline_state()


def save_state(items: list[dict[str, str]]) -> None:
    data = {
        "initialized": True,
        "current_items": [
            {"key": item["key"], "title": item["title"], "url": item["url"], "kind": item["kind"]}
            for item in sorted(items, key=lambda x: (x["title"].casefold(), x["url"]))
        ],
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send_telegram(message: str) -> bool:
    """Send one message to both recipients. Do not print the token on failure."""
    if not BOT_TOKEN:
        print("[ОШИБКА] Не задан GitHub secret TELEGRAM_BOT_TOKEN.")
        return False
    if len(CHAT_IDS) != 2:
        print("[ОШИБКА] Не заданы оба chat_id: мамы и папы.")
        return False

    all_ok = True
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
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
            body = response.json()
            if body.get("ok") is not True:
                all_ok = False
                print(f"[Telegram] Telegram не подтвердил отправку получателю #{index}.")
        except Exception as exc:
            all_ok = False
            print(f"[Telegram] Ошибка отправки получателю #{index}: {type(exc).__name__}.")
    return all_ok


def fetch_relevant_items() -> list[dict[str, str]]:
    """Fetch and parse the official Kubcenter compensation index."""
    response = requests.get(
        SECTION_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    page_text = normalize(soup.get_text(" ", strip=True))
    # Fail closed if we received an error/challenge page or unexpected redirect.
    if "компенсационные выплаты" not in page_text:
        raise RuntimeError("Ответ не похож на официальную страницу «Компенсационные выплаты».")

    found: dict[str, dict[str, str]] = {}

    # 1) Titles of links added to the page. The current site publishes intake
    # orders as linked entries; those titles often start with "Приказ о ...".
    for anchor in soup.find_all("a", href=True):
        title = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        if not title or not relevant_announcement(title):
            continue
        href = urljoin(SECTION_URL, anchor["href"].strip())
        parsed_href = urlparse(href)
        if parsed_href.scheme not in {"http", "https"}:
            continue
        item = canonical_item(title, href, "ссылка на публикацию")
        found[item["key"]] = item

    # 2) Relevant page headings themselves. This catches a future page where
    # the notice is rendered as a heading rather than as a linked menu item.
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        title = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip()
        if not title or not relevant_announcement(title):
            continue
        nested_link = heading.find("a", href=True)
        href = urljoin(SECTION_URL, nested_link["href"].strip()) if nested_link else SECTION_URL
        item = canonical_item(title, href, "заголовок страницы")
        found[item["key"]] = item

    # 3) Accessible image alternative text. Some public notices use an image
    # banner, so capture it if the alt text itself identifies the intake notice.
    for image in soup.find_all("img"):
        title = re.sub(r"\s+", " ", (image.get("alt") or "")).strip()
        src = (image.get("src") or "").strip()
        if not title or not src or not relevant_announcement(title):
            continue
        href = urljoin(SECTION_URL, src)
        item = canonical_item(title, href, "баннер объявления")
        found[item["key"]] = item

    print(
        f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] "
        f"Официальная страница доступна; релевантных публикаций/заголовков: {len(found)}."
    )
    for item in sorted(found.values(), key=lambda x: x["title"].casefold()):
        print(f"  - {item['title']} | {item['url']}")
    return list(found.values())


def make_alert(items: list[dict[str, str]]) -> str:
    stamp = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S %Z")
    excerpts = "\n".join(f"• {item['title']}\n{item['url']}" for item in items[:8])
    return (
        "🚨 НОВАЯ ПУБЛИКАЦИЯ О ПРИЁМЕ КОМПЕНСАЦИИ ЗУ\n\n"
        "На официальном сайте Кубанского центра появилась новая или изменённая "
        "публикация, похожая на объявление о приёме/продлении приёма заявлений.\n\n"
        f"Время обнаружения: {stamp}\n\n"
        f"Что обнаружено:\n{excerpts}\n\n"
        f"Раздел Кубцентра: {SECTION_URL}\n"
        f"Портал подачи заявления: {APPLICATION_URL}\n\n"
        "Важно: это сигнал проверить приказ и форму вручную; сам по себе заголовок "
        "не доказывает, что приём открыт именно сейчас или что заявитель подходит под условия."
    )


def validate_secrets() -> bool:
    if not BOT_TOKEN:
        print("[ОШИБКА] Добавь secret TELEGRAM_BOT_TOKEN в Settings → Secrets and variables → Actions.")
        return False
    if len(CHAT_IDS) != 2:
        print("[ОШИБКА] Добавь secrets TELEGRAM_CHAT_ID_MOM и TELEGRAM_CHAT_ID_DAD.")
        return False
    return True


def send_test() -> int:
    if not validate_secrets():
        return 2
    message = (
        "✅ Тест облачного монитора компенсации ЗУ.\n\n"
        "Telegram-связь настроена. Монитор проверяет официальный раздел публикаций "
        "Кубанского центра и сообщит о новой/изменённой публикации, связанной с "
        "приёмом или продлением приёма заявлений.\n\n"
        f"Раздел: {SECTION_URL}\nПортал заявления: {APPLICATION_URL}\n\n"
        "Тест не подтверждает, что приём открыт прямо сейчас."
    )
    return 0 if send_telegram(message) else 1


def run_monitor() -> int:
    if not validate_secrets():
        return 2

    try:
        current_items = fetch_relevant_items()
    except Exception as exc:
        # Do not update the baseline on a failed/blocked response. That avoids
        # interpreting an error page as disappearance of all announcements.
        print(f"[ОШИБКА] Проверка не удалась: {type(exc).__name__}: {exc}")
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
    removed_keys = set(previous_by_key) - set(current_by_key)

    # If state has somehow lost both old items and initialization, don't treat
    # every existing archive entry as new: compare with the known live baseline.
    if not state.get("initialized"):
        previous_by_key = {
            item["key"]: item for item in baseline_state()["current_items"]
        }
        new_keys = set(current_by_key) - set(previous_by_key)
        removed_keys = set(previous_by_key) - set(current_by_key)

    new_items = [current_by_key[key] for key in sorted(new_keys)]
    if new_items:
        message = make_alert(new_items)
        print("Найдена новая релевантная публикация; отправляю уведомления.")
        if not send_telegram(message):
            print("[ОШИБКА] Не всем получателям удалось отправить сообщение. Состояние не сохраняю — повторю попытку при следующем запуске.")
            return 1
        print("Уведомления успешно отправлены обоим получателям.")
    else:
        print("Новых релевантных публикаций нет.")

    if removed_keys:
        print(f"Исчезнувших публикаций/заголовков: {len(removed_keys)}; состояние обновляется.")

    # Save only after successful site fetch and, when needed, successful alerts.
    save_state(current_items)
    print("Состояние мониторинга сохранено.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Монитор официальных объявлений о компенсации взамен земельного участка.")
    parser.add_argument("--test", action="store_true", help="Отправить тестовое сообщение двум получателям Telegram")
    args = parser.parse_args()
    if args.test:
        return send_test()
    return run_monitor()


if __name__ == "__main__":
    sys.exit(main())
