"""scrape_cli — аргументы командной строки и диагностика."""
from __future__ import annotations

import argparse
import logging
from redcat.quality import anomalies as anomaly_lib
from redcat.sources import registry as src
from redcat.collection.scrape_config import DATA_DB, REQUEST_TIMEOUT
from redcat.collection.scrape_env import requests
from redcat.collection.scrape_pages import find_total
from redcat.collection.scrape_token import resolve_token


def build_arg_parser():
    p = argparse.ArgumentParser(description="RedCat Scraper — универсальный сборщик по API")
    p.add_argument("--token", default=None, help="Токен на этот запуск (перекрывает .env)")
    p.add_argument("--only", nargs="+", metavar="SOURCE",
                   help="Собрать только эти источники (зависимости подтянутся)")
    p.add_argument("--list-sources", action="store_true",
                   help="Показать зарегистрированные источники и выйти")
    p.add_argument("--excel", action="store_true",
                   help="Дополнительно записать .xlsx")
    p.add_argument("--no-anomalies", action="store_true", help="Пропустить поиск аномалий")
    p.add_argument("--sensitivity", type=float, default=anomaly_lib.DEFAULT_Z,
                   help=f"Порог z-score для аномалий (по умолчанию {anomaly_lib.DEFAULT_Z})")
    p.add_argument("--concurrency", type=int, default=None,
                   help="Переопределить параллельность дробящихся источников")
    p.add_argument("--diagnose", nargs="*", metavar="SOURCE",
                   help="Проверить источники запрос за запросом")
    p.add_argument("--fail-on-critical", action="store_true",
                   help="Выйти с кодом 2 при критичных аномалиях")
    return p


def run_diagnostics(keys, cli_token, params) -> int:
    from redcat.collection import diagnose as diag

    token = resolve_token(cli_token)
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}",
                            "Accept": "application/json"})

    specs = src.all_sources(only=keys or None)
    if not specs:
        print("❌ Нет источников для проверки.")
        return 1

    print("\n🩺 ДИАГНОСТИКА ИСТОЧНИКОВ")
    print(f"   Проверяю: {', '.join(s.key for s in specs)}")

    parents, reports = {}, []
    for spec in specs:
        if spec.split_values_from and spec.split_values_from not in parents:
            parent = src.get(spec.split_values_from)
            if parent:
                try:
                    url = parent.resolved_url(params)
                    sep = "&" if "?" in url else "?"
                    resp = session.get(
                        f"{url}{sep}{parent.page_size_param}=25"
                        f"&{parent.page_number_param}=1", timeout=REQUEST_TIMEOUT)
                    rows = src.dig(resp.json(), parent.data_path)
                    parents[spec.split_values_from] = rows if isinstance(rows, list) else []
                except Exception as e:  # noqa: BLE001
                    logging.warning("Диагностика: родитель %s недоступен: %s",
                                    spec.split_values_from, e)
                    parents[spec.split_values_from] = []

        report = diag.diagnose_source(spec, session, params, DATA_DB,
                                      find_total, parents)
        report.print()
        reports.append(report)

    broken = [r for r in reports if r.problems]
    print(f"\n{'═' * 62}")
    if broken:
        print(f"❌ Поломок найдено: {', '.join(r.key for r in broken)}")
        return 2
    risky = [r for r in reports if any(l["level"] == "⚠️" for l in r.lines)]
    if risky:
        print(f"⚠️ Поломок нет, но есть предупреждения: "
              f"{', '.join(r.key for r in risky)}")
        return 0
    print("✅ Все источники проверены, замечаний нет.")
    return 0
