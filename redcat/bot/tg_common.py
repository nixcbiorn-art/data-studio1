"""tg_common — пути, константы, логгер и общее состояние Telegram-бота."""
from __future__ import annotations

from redcat.core import runner
from redcat.core import paths
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
import logging

# ──────────────────────────────────────────────────────────────
#  Пути и константы
# ──────────────────────────────────────────────────────────────
HERE = paths.ROOT

ENV = HERE / ".env"

REPORTS = HERE / "reports"

EXPORT_SCRIPT = runner.script_path("export_problem_jc.py")

EXPORT_LOG = HERE / "tg_bot_export.log"

BOT_LOG = HERE / "tg_bot.log"

STATE = HERE / "tg_bot_state.json"

# Роли и права пользователей.
try:
    from redcat.bot import bot_users
except ImportError:
    bot_users = None

# Имя бота — заполняется в poll_loop после getMe,
# нужно для ссылок-приглашений.
_BOT_USERNAME = ""

BOT_STARTED_AT = datetime.now()

API = "https://api.telegram.org"

POLL_TIMEOUT = 25

REQUEST_TIMEOUT = 30

WATCH_INTERVAL = 30

WATCH_BASELINE_MTIME_OFFSET = 5

# ──────────────────────────────────────────────────────────────
#  Логгер
# ──────────────────────────────────────────────────────────────
def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("tg_bot")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s  %(levelname)-5s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    try:
        fh = RotatingFileHandler(str(BOT_LOG), maxBytes=5 * 1024 * 1024,
                                 backupCount=1, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    try:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    except Exception:
        pass
    return logger

LOG = _setup_logger()

def log_startup_environment() -> None:
    try:
        from platform import python_version, system, release
        LOG.info("=" * 60)
        LOG.info("Запуск tg_bot.py")
        LOG.info("  Python: %s (%s %s)", python_version(), system(), release())
        LOG.info("  Cwd:    %s", HERE)
        LOG.info("  PID:    %s", os.getpid())
        LOG.info("  Лог:    %s", BOT_LOG)
        LOG.info("=" * 60)
    except Exception as e:
        LOG.warning("не удалось записать окружение: %s", e)