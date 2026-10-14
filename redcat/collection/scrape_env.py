"""scrape_env — проверка зависимостей, необязательные импорты, загрузка .env и состояние токена."""
from __future__ import annotations

import importlib.util
import logging
import os
import re
from redcat.core import api_guard


# ──────────────────────────────────────────────────────────────
#  ЗАВИСИМОСТИ
# ──────────────────────────────────────────────────────────────
REQUIRED_PACKAGES = {
    "aiohttp": "aiohttp",
    "requests": "requests",
    "pandas": "pandas",
}


def missing_dependencies() -> list:
    return [pkg for module, pkg in REQUIRED_PACKAGES.items()
            if importlib.util.find_spec(module) is None]


def print_dependency_help(missing) -> None:
    print("❌ Для сбора данных не хватает библиотек: " + ", ".join(missing))
    print("\n   Установите все зависимости одной командой:")
    print("       pip install -r requirements.txt")
    print(f"\n   Или только недостающие:")
    print(f"       pip install {' '.join(missing)}")
    print("\n   ℹ️ Просмотр и анализ уже собранных данных работает без них:")
    print("       python studio.py")


_MISSING = missing_dependencies()


try:
    import requests
except ImportError:
    requests = None


try:
    import aiohttp
except ImportError:
    aiohttp = None


try:
    from redcat.data import storage
except ImportError:
    storage = None


# Модуль виртуального браузера — необязательная зависимость.
try:
    from redcat.collection import browser_fetch
except ImportError:
    browser_fetch = None


api_guard.install()


_TOKEN_SOURCE = None


_ENV_FILE_USED = None


_SHADOWED_OS_TOKEN = False


_ENV_BAD_LINES = []


def clean_token(raw):
    t = re.sub(r"\s+", "", raw or "").strip("\"'")
    if t.lower().startswith("bearer"):
        t = t[6:]
    return t.strip("\"'")


_os_token_before = clean_token(os.environ.get("REDCAT_TOKEN", ""))


try:
    from dotenv import dotenv_values, find_dotenv

    class _DotenvWarnCatcher(logging.Handler):
        def emit(self, record):
            m = re.search(r"line (\d+)", record.getMessage())
            if m:
                _ENV_BAD_LINES.append(int(m.group(1)))

    _dl = logging.getLogger("dotenv.main")
    _dl.addHandler(_DotenvWarnCatcher())
    _dl.propagate = False

    _env_path = find_dotenv()
    _vals = dotenv_values(_env_path) if _env_path else {}
    for _k, _v in _vals.items():
        if _v is not None and _k not in os.environ:
            os.environ[_k] = _v
    _env_token = clean_token(_vals.get("REDCAT_TOKEN"))
    if _env_token and "вставьте" not in _env_token.lower():
        os.environ["REDCAT_TOKEN"] = _env_token
        _TOKEN_SOURCE, _ENV_FILE_USED = ".env", _env_path
        _SHADOWED_OS_TOKEN = bool(_os_token_before) and _os_token_before != _env_token
    elif os.environ.get("REDCAT_TOKEN"):
        _TOKEN_SOURCE = "окружение ОС"
except ImportError:
    if _os_token_before:
        _TOKEN_SOURCE = "окружение ОС"
