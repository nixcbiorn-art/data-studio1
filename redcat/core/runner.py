"""Запуск дочерних скриптов проекта через `python -m`.

Раньше скрипты лежали в корне и запускались по имени файла. Теперь они в
пакетах, поэтому имя файла («redcat_scraper.py») сопоставляется с модулем.
"""
from __future__ import annotations

import sys
from pathlib import Path

from redcat.core.paths import ROOT

SCRIPT_MODULES = {
    "anomalies.py": "redcat.quality.anomalies",
    "api_guard.py": "redcat.core.api_guard",
    "bot_users.py": "redcat.bot.bot_users",
    "browser_fetch.py": "redcat.collection.browser_fetch",
    "check_runtime.py": "redcat.tools.check_runtime",
    "collect_pool.py": "redcat.collection.collect_pool",
    "completeness.py": "redcat.quality.completeness",
    "completeness_dashboard.py": "redcat.reporting.completeness_dashboard",
    "crosschecks.py": "redcat.quality.crosschecks",
    "dataops.py": "redcat.data.dataops",
    "demo_data.py": "redcat.tools.demo_data",
    "diagnose.py": "redcat.collection.diagnose",
    "export_problem_jc.py": "redcat.reporting.export_problem_jc",
    "field_labels.py": "redcat.data.field_labels",
    "field_semantics.py": "redcat.data.field_semantics",
    "fill_aliases.py": "redcat.tools.fill_aliases",
    "hc_aliases.py": "redcat.sources.hc_aliases",
    "hc_crosscheck.py": "redcat.quality.hc_crosscheck",
    "history_dashboard.py": "redcat.reporting.history_dashboard",
    "jc_compare.py": "redcat.reporting.jc_compare",
    "launcher.py": "redcat.tools.launcher",
    "make_field_labels.py": "redcat.tools.make_field_labels",
    "match_complex_names.py": "redcat.tools.match_complex_names",
    "name_normalizer.py": "redcat.sources.name_normalizer",
    "quality.py": "redcat.quality.drift",
    "rate_limit.py": "redcat.collection.rate_limit",
    "redcat_scraper.py": "redcat.collection.redcat_scraper",
    "report_html.py": "redcat.reporting.report_html",
    "run_stats.py": "redcat.data.run_stats",
    "scan_sources.py": "redcat.tools.scan_sources",
    "selftest_readonly.py": "redcat.tools.selftest_readonly",
    "set_telegram.py": "redcat.tools.set_telegram",
    "set_token.py": "redcat.tools.set_token",
    "source_stats.py": "redcat.reporting.source_stats",
    "sources.py": "redcat.sources.registry",
    "spec_validator.py": "redcat.sources.spec_validator",
    "sql.py": "redcat.tools.sql",
    "storage.py": "redcat.data.storage",
    "studio_store.py": "redcat.data.studio_store",
    "tg_bot.py": "redcat.bot.tg_bot",
    "thresholds.py": "redcat.data.thresholds",
    "webapp.py": "redcat.web.webapp"
}


def script_path(script: str) -> Path:
    """Путь к файлу скрипта (несуществующий путь, если скрипта нет)."""
    mod = SCRIPT_MODULES.get(script)
    if not mod:
        return ROOT / "_missing_" / script
    return ROOT / (mod.replace(".", "/") + ".py")


def script_cmd(script: str, *args, unbuffered: bool = False) -> list[str] | None:
    """Команда запуска скрипта или None, если такого скрипта нет."""
    mod = SCRIPT_MODULES.get(script)
    if not mod or not script_path(script).exists():
        return None
    cmd = [sys.executable]
    if unbuffered:
        cmd.append("-u")
    cmd += ["-m", mod]
    cmd += [str(a) for a in args]
    return cmd
