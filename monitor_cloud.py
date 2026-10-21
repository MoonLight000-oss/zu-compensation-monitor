"""Monitor official Kubcenter compensation announcements and notify Telegram."""
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
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

CHAT_IDS = [
    os.getenv("TELEGRAM_CHAT_ID_MOM", "").strip(),
    os.getenv("TELEGRAM_CHAT_ID_DAD", "").strip(),
]
CHAT_IDS = [value for value in CHAT_IDS if value]

SECTION_NAME = "Компенсационные выплаты — официальный сайт Кубцентра"

SECTION_URL = (
    "https://xn--90ainopldz.xn--p1ai/"
    "%D0%BA%D0%BE%D0%BC%D0%BF%D0%B5%D0%BD%D1%81%D0%B0%D1%86"
    "%D0%B8%D0%BE%D0%BD%D0%BD%D1%8B%D0%B5-%D0%B2%D1%8B%D0%"
    B%D0%BF%D0%BB%D0%B0%D1%82%D1%8B/"
)

APPLICATION_URL = "https://gpn.krasnodar.ru/"

STATE_PATH = Path("monitor_state.json
