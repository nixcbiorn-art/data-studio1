"""Единственное место, где определены пути проекта."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = ROOT / "config"
SOURCES_DIR = CONFIG_DIR / "sources"
SOURCES_EXT_DIR = CONFIG_DIR / "sources_external"

WEB_DIR = ROOT / "redcat" / "web" / "static"

# Данные, создаваемые при работе (не в git)
REPORTS_DIR = ROOT / "reports"
HISTORY_DIR = ROOT / "history"
ENV_FILE = ROOT / ".env"
STUDIO_DB = ROOT / "studio.db"
