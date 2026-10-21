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

SECTION_URL = "https://xn--90ainopldz.xn--p1ai/компенсационные-выплаты/"

APPLICATION_URL = "https://gpn.krasnodar.ru/"

STATE_PATH = Path("monitor_state.json
