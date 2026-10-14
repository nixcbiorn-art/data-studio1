"""scrape_config — пути, БД, логирование и параметры запросов из окружения."""
from __future__ import annotations

from redcat.collection import scrape_env  # noqa: F401  — первым: загрузка .env до чтения переменных
from redcat.core import paths
import logging
import os
from redcat.core import api_guard


# ──────────────────────────────────────────────────────────────
#  ПУТИ
# ──────────────────────────────────────────────────────────────
BASE_DIR = paths.ROOT


HISTORY_DIR = BASE_DIR / "history"


OUTPUT_DIR = BASE_DIR / "reports"


SOURCES_DIR = paths.SOURCES_DIR


SOURCES_EXT_DIR = paths.SOURCES_EXT_DIR


HISTORY_DIR.mkdir(exist_ok=True)


OUTPUT_DIR.mkdir(exist_ok=True)


DATA_DB = OUTPUT_DIR / "redcat_data.db"


EXTERNAL_DB = OUTPUT_DIR / "external_data.db"


STATS_DB = HISTORY_DIR / "run_stats.db"


DASHBOARD_FILE = OUTPUT_DIR / "dashboard.html"


STUDIO_DB = BASE_DIR / "studio.db"


try:
    from redcat.data import studio_store as _studio_store

    _studio_store.init(STUDIO_DB)
    api_guard.set_audit_hook(
        lambda m, u, s, ms, note="": _studio_store.log_api(STUDIO_DB, m, u, s, ms, note))
except Exception:  # noqa: BLE001
    pass


logging.basicConfig(
    filename=str(BASE_DIR / "redcat_scraper.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)


REQUEST_TIMEOUT = int(os.environ.get("REDCAT_TIMEOUT", "20"))


MAX_RETRIES = int(os.environ.get("REDCAT_MAX_RETRIES", "3"))


RETRY_DELAY_SEC = 2


KEEP_RECORD_HISTORY_RUNS = int(os.environ.get("REDCAT_KEEP_HISTORY_RUNS", "30"))
