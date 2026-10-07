"""web_config — пути, БД, защита API (api_guard), общие настройки веб-приложения."""
from __future__ import annotations

from redcat.core import paths
from redcat.core import api_guard
from redcat.data import studio_store as store


try:
    from redcat.data.thresholds import percent_threshold as _pct_thr
except ImportError:
    def _pct_thr(size, base_pct=2.0, **kw):
        return base_pct


BASE_DIR = paths.ROOT


WEB_DIR = paths.WEB_DIR


OUTPUT_DIR = BASE_DIR / "reports"


HISTORY_DIR = BASE_DIR / "history"


SOURCES_DIR = paths.SOURCES_DIR


SOURCES_EXT_DIR = paths.SOURCES_EXT_DIR


DATA_DB = OUTPUT_DIR / "redcat_data.db"


EXTERNAL_DB = OUTPUT_DIR / "external_data.db"


STATS_DB = HISTORY_DIR / "run_stats.db"


STUDIO_DB = BASE_DIR / "studio.db"


ENV_FILE = BASE_DIR / ".env"


api_guard.install()


api_guard.set_audit_hook(
    lambda m, u, s, ms, note="": store.log_api(STUDIO_DB, m, u, s, ms, note))
