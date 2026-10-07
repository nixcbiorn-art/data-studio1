"""
RedCat Scraper — универсальный сборщик данных по API
====================================================
Собирает любые описанные источники, нормализует, сравнивает с прошлым
запуском, ищет аномалии, проверяет качество данных и выгружает всё в
SQLite/Parquet + HTML-дашборд.

Источники описываются декларативно в папках:
  • sources/           — Redcat (ходит с Bearer-токеном)
  • sources_external/  — внешние (свой режим сбора, своя база)

Быстрый старт:
  pip install -r requirements.txt
  python -m redcat.collection.redcat_scraper --token "eyJ...ваш_токен"

Полезные флаги:
  --list-sources          показать все зарегистрированные источники
  --only apartments       собрать только указанные (зависимости подтянутся)
  --only fsk_apartments   собирает внешний источник без Redcat-токена
  --excel                 дополнительно записать .xlsx
  --no-anomalies          пропустить поиск аномалий
  --sensitivity 2.5       чувствительность аномалий (меньше = строже)
  --fail-on-critical      выйти с кодом 2 при критичных аномалиях

Токен НИКОГДА не хранится в скрипте: --token или REDCAT_TOKEN в .env.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from redcat.quality import anomalies as anomaly_lib
from redcat.core import api_guard
from redcat.quality import drift as quality
from redcat.reporting import report_html
from redcat.data import run_stats
from redcat.sources import registry as src
from redcat.collection.scrape_cli import build_arg_parser, run_diagnostics
from redcat.collection.scrape_collect import collect_source
from redcat.collection.scrape_config import DASHBOARD_FILE, DATA_DB, EXTERNAL_DB, KEEP_RECORD_HISTORY_RUNS, OUTPUT_DIR, SOURCES_DIR, SOURCES_EXT_DIR, STATS_DB
from redcat.collection.scrape_enrich import _enrich_apartments_with_hc
from redcat.collection.scrape_env import _MISSING, print_dependency_help, requests, storage
from redcat.collection.scrape_snapshot import compare_snapshots, load_snapshot, save_snapshot
from redcat.collection.scrape_token import resolve_token



# ── Обратная совместимость ─────────────────────────────────────────────
# Раньше всё жило в одном файле (rs.collect_source, launcher: decode_jwt_payload и т. д.).
from redcat.collection.scrape_browser import (  # noqa: F401
    _collect_source_browser_sync,
    _collect_source_browser_xhr,
    _extract_items_from_html,
    _fetch_pages_browser_async,
    collect_source_browser,
)
from redcat.collection.scrape_config import (  # noqa: F401
    BASE_DIR,
    HISTORY_DIR,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    RETRY_DELAY_SEC,
    STUDIO_DB,
    _studio_store,
)
from redcat.collection.scrape_env import (  # noqa: F401
    REQUIRED_PACKAGES,
    _ENV_BAD_LINES,
    _ENV_FILE_USED,
    _SHADOWED_OS_TOKEN,
    _TOKEN_SOURCE,
    _os_token_before,
    aiohttp,
    browser_fetch,
    clean_token,
    dotenv_values,
    find_dotenv,
    missing_dependencies,
)
from redcat.collection.scrape_guard import (  # noqa: F401
    DEFAULT_CIRCUIT_COOLDOWN,
    DEFAULT_CIRCUIT_FAILURES,
    DEFAULT_RATE_RPS,
    _BREAKER_LOCK,
    _CIRCUIT_BREAKERS,
    _RATE_LIMITERS,
    _breaker_for,
    _rps_for,
    circuit_breaker_snapshot,
)
from redcat.collection.scrape_http import (  # noqa: F401
    _fetch_http_async,
    _fetch_http_sync,
)
from redcat.collection.scrape_pages import (  # noqa: F401
    TOTAL_PATHS,
    _fetch_json,
    _looks_deterministic,
    _next_url,
    _set_query_param,
    _window_limit_hint,
    fetch_pages,
    find_total,
)
from redcat.collection.scrape_probe import (  # noqa: F401
    probe_live_values,
    probe_total,
)
from redcat.collection.scrape_split import (  # noqa: F401
    ES_WINDOW,
    _fetch_one_split,
    _get_field,
    _norm_name,
    _rows_of,
    _total_of,
    fetch_split_async,
    preflight_check_split,
)
from redcat.collection.scrape_token import (  # noqa: F401
    decode_jwt_payload,
)


def main():
    args = build_arg_parser().parse_args()
    run_started_at = datetime.now()

    loaded = src.load_from_dir(SOURCES_DIR)
    loaded_ext = src.load_from_dir(SOURCES_EXT_DIR)
    if loaded_ext:
        print(f"📦 Внешних источников загружено: {loaded_ext} "
              f"(папка {SOURCES_EXT_DIR.name}/)")
    params = src.env_params()

    if args.list_sources:
        print(f"Зарегистрировано источников: {loaded + loaded_ext}\n")
        for s in src.all_sources():
            tags = []
            if s.external:
                tags.append("[внешний]")
            if s.split_values_from:
                tags.append(f"← дробится по {s.split_values_from}")
            if getattr(s, "fetch_mode", "http") == "browser":
                tags.append("🌐 браузер")
            tag = ("  " + "  ".join(tags)) if tags else ""
            print(f"  • {s.key:20} {s.title or ''}{tag}")
            # Для split-источников показываем шаблон, а не url с {value}
            if s.split_url_template:
                print(f"    {s.split_url_template[:110]}")
            else:
                try:
                    print(f"    {s.resolved_url(params)[:110]}")
                except ValueError as e:
                    print(f"    ⚠️ {e}")
        if _MISSING:
            print(f"\n⚠️ Для самого сбора не хватает: {', '.join(_MISSING)}")
        return 0

    if _MISSING:
        print_dependency_help(_MISSING)
        return 1

    if args.diagnose is not None:
        return run_diagnostics(args.diagnose, args.token, params)

    print("🚀 REDCAT SCRAPER")
    print("=" * 56)
    print(api_guard.status_text())
    print(f"📋 Источников загружено: {loaded + loaded_ext}")

    specs_all = src.all_sources(only=args.only)
    redcat_specs = [s for s in specs_all if not s.external]

    if redcat_specs:
        token = resolve_token(args.token)
    else:
        token = ""
        print("🔓 Собираются только внешние источники — Redcat-токен не требуется.")

    redcat_session = requests.Session()
    redcat_session.trust_env = False  # игнорировать HTTP_PROXY/HTTPS_PROXY
    redcat_session.headers.update({"Authorization": f"Bearer {token}",
                                   "Accept": "application/json"})
    external_session = requests.Session()
    external_session.trust_env = False  # игнорировать HTTP_PROXY/HTTPS_PROXY
    external_session.headers.update({"Accept": "application/json"})

    timestamp = run_started_at.strftime("%Y%m%d_%H%M")
    specs = specs_all
    if not specs:
        print("❌ Нет источников для сбора. Проверьте папки sources/, "
              "sources_external/ и --only.")
        return 1
    if args.concurrency:
        for spec in specs:
            if spec.split_param:
                spec.concurrency = args.concurrency
        print(f"  ⚙️ Параллельность переопределена: {args.concurrency}")

    # ── 1. Сбор ──
    print(f"\n1️⃣ Сбор данных ({len(specs)} источник(ов))...")
    raw, normalized, totals, any_incomplete = {}, {}, {}, False
    incomplete_by_spec = {}
    for spec in specs:
        session = external_session if spec.external else redcat_session
        try:
            rows, inc, total = collect_source(spec, session, token, params, raw)
        except ValueError as e:
            print(f"  ❌ [{spec.key}] {e}")
            logging.error("[%s] %s", spec.key, e)
            any_incomplete = True
            continue
        if not rows and inc:
            print(f"  ⚠️ [{spec.key}] не собран — история и данные прошлого "
                  f"запуска сохранены, сравнение и аномалии по нему пропущены.")
            any_incomplete = True
            continue
        raw[spec.key] = rows
        totals[spec.key] = total
        any_incomplete |= inc
        incomplete_by_spec[spec.key] = inc
        normalized[spec.key] = src.normalize(rows, spec)

    # Обогащаем apartments связью с ЖК (housing_complex_id, name),
    # взяв её из apartments_estates + housing_complexes.
    _enrich_apartments_with_hc(normalized)

    if not normalized:
        print("❌ Не собрано ни одного источника.")
        return 1

    # ── 2. Сравнение с прошлым запуском ──
    print("\n2️⃣ Сравнение с прошлым запуском...")
    all_changes, new_snapshots, prev_snapshots = [], {}, {}
    for spec in specs:
        if spec.key not in normalized:
            continue
        prev = load_snapshot(spec.key)
        prev_snapshots[spec.key] = prev
        if incomplete_by_spec.get(spec.key):
            print(f"  ⚠️ {spec.key}: сбор неполный — сравнение пропущено.")
            continue
        changes, snap = compare_snapshots(prev, normalized[spec.key], spec)
        new_snapshots[spec.key] = snap
        all_changes.extend(changes)
        if prev:
            print(f"  • {spec.key}: изменений {len(changes)}")
    is_first_run = not any(prev_snapshots.values())
    if is_first_run:
        print("  ℹ️ Первый запуск — сравнивать не с чем.")
    else:
        print(f"  🔄 Всего изменений: {len(all_changes)}")

    # ── 3. Качество данных ──
    print("\n3️⃣ Проверка качества данных...")
    quality_issues, profiles = [], {}
    for spec in specs:
        if spec.key not in normalized:
            continue
        prof = quality.profile(normalized[spec.key])
        profiles[spec.key] = prof
        old_prof = quality.load_profile(STATS_DB, spec.key)
        quality_issues.extend(quality.compare_profiles(old_prof, prof, spec.key))
    print(f"  🔍 Замечаний по схеме и заполненности: {len(quality_issues)}")

    # ── 4. Поиск аномалий ──
    found = []
    if not args.no_anomalies:
        print("\n4️⃣ Поиск аномалий...")
        found.extend(quality_issues)
        for spec in specs:
            if spec.key not in normalized:
                continue
            rows = normalized[spec.key]
            found.extend(anomaly_lib.detect_data_anomalies(
                rows, spec, z_threshold=args.sensitivity))
            found.extend(anomaly_lib.detect_identical_value_clusters(
                rows, spec))
            if not incomplete_by_spec.get(spec.key):
                found.extend(anomaly_lib.detect_record_change_anomalies(
                    prev_snapshots.get(spec.key), rows, spec))
                found.extend(anomaly_lib.detect_group_disappearance(
                    prev_snapshots.get(spec.key), rows, spec))
        print(f"  🔎 Найдено на уровне данных: {len(found)}")
    else:
        print("\n4️⃣ Поиск аномалий пропущен (--no-anomalies).")

    # ── 5. Выгрузка ──
    print("\n5️⃣ Сохранение данных...")
    skipped_notes = [
        {"Категория": f"{(src.get(key).title or key)}: сравнение пропущено",
         "ID": "", "Название": "", "Поле": "",
         "Было": "источник собран не полностью", "Стало": ""}
        for key in incomplete_by_spec if incomplete_by_spec.get(key) and key in normalized
    ]
    comparison_rows = all_changes + skipped_notes or [{"Информация":
        "Первый запуск — сравнивать не с чем" if is_first_run else "Изменений не найдено"}]

    redcat_tables = {k: v for k, v in normalized.items()
                     if not (src.get(k) and src.get(k).external)}
    external_tables = {k: v for k, v in normalized.items()
                       if src.get(k) and src.get(k).external}
    redcat_tables["comparison_vs_previous"] = comparison_rows
    if external_tables:
        external_tables["comparison_vs_previous"] = comparison_rows

    files_written = []
    storage.write_sqlite(DATA_DB, redcat_tables)
    files_written.append(DATA_DB)
    print(f"  🗄️ SQLite (Redcat): {DATA_DB.name}")
    if external_tables:
        storage.write_sqlite(EXTERNAL_DB, external_tables)
        files_written.append(EXTERNAL_DB)
        print(f"  🗄️ SQLite (внешние): {EXTERNAL_DB.name}")

    columnar = storage.write_columnar(OUTPUT_DIR, redcat_tables, timestamp)
    if external_tables:
        columnar += storage.write_columnar(OUTPUT_DIR, external_tables,
                                           f"{timestamp}_ext")
    files_written.extend(columnar)
    if columnar:
        kind = "Parquet" if columnar[0].suffix == ".parquet" else "CSV"
        print(f"  📦 {kind}: {len(columnar)} файл(ов)")

    if args.excel:
        ok, warns = storage.write_excel(
            OUTPUT_DIR / f"redcat_export_{timestamp}.xlsx",
            {k[:31]: v for k, v in redcat_tables.items()})
        for w in warns:
            print(f"  ⚠️ {w}")
        if ok:
            print("  📊 Excel записан")

    for key, snap in new_snapshots.items():
        save_snapshot(key, snap)

    try:
        from redcat.quality import hc_crosscheck
        apt_spec = next((sp for sp in specs if sp.key == "apartments"), None) or src.get("apartments")
        hc_crosscheck.run_from_scraper(normalized, load_snapshot, apt_spec, OUTPUT_DIR, timestamp)
    except Exception:  # noqa: BLE001
        logging.exception("Перекрёстная проверка ЖК не выполнена")
        print("  ⚠️ Перекрёстная проверка ЖК не выполнена — подробности в redcat_scraper.log")

    # ── 6. Метрики запуска ──
    print("\n6️⃣ История запусков...")
    primary = max(normalized, key=lambda k: len(normalized[k]))
    metrics = run_stats.compute_generic_metrics(
        normalized=normalized, totals=totals, changes=all_changes,
        incomplete=any_incomplete, started_at=run_started_at,
        finished_at=datetime.now(), primary=primary,
        primary_spec=src.get(primary),
        all_specs={sp.key: sp for sp in specs},
    )
    run_id = run_stats.save_run(STATS_DB, metrics)

    for spec in specs:
        if spec.key in profiles:
            quality.save_profile(STATS_DB, spec.key, run_id, profiles[spec.key])
        if spec.key in normalized and spec.numeric_fields:
            storage.append_record_history(
                STATS_DB, spec.key, run_id, normalized[spec.key],
                spec.id_field, spec.numeric_fields,
                run_started_at.isoformat(timespec="seconds"))
    storage.prune_record_history(STATS_DB, KEEP_RECORD_HISTORY_RUNS)

    history = run_stats.load_history(STATS_DB, limit=100)

    if not args.no_anomalies:
        found.extend(anomaly_lib.detect_history_anomalies(
            history, z_threshold=args.sensitivity))
        for spec in specs:
            if spec.key not in normalized or incomplete_by_spec.get(spec.key):
                continue
            found.extend(anomaly_lib.detect_frozen_records(
                STATS_DB, spec.key, normalized[spec.key], spec))
    summary = anomaly_lib.summarize(found)
    storage.save_anomalies(STATS_DB, run_id, summary["items"],
                           run_started_at.isoformat(timespec="seconds"))
    print(f"  📈 Запуск #{run_id} записан (всего запусков: {len(history)}).")

    # ── 7. Дашборд ──
    dashboard = report_html.build_dashboard(
        history, DASHBOARD_FILE, files_written, summary,
        storage.load_anomaly_counts(STATS_DB))
    if dashboard:
        print(f"  📊 Дашборд: {dashboard}")

    # ── Итог ──
    print("\n" + "=" * 56)
    for key, rows in normalized.items():
        tag = " [внешний]" if (src.get(key) and src.get(key).external) else ""
        delta = run_stats.format_delta(history, f"{key}_count") if len(history) > 1 else ""
        print(f"   {key}: {len(rows)}{delta}{tag}")
    counts = summary["counts"]
    if summary["total"]:
        print(f"\n🔎 Аномалии: критичных {counts.get('critical', 0)}, "
              f"предупреждений {counts.get('warning', 0)}, "
              f"информационных {counts.get('info', 0)}")
        for a in summary["items"][:5]:
            icon = {"critical": "🔴", "warning": "🟡"}.get(a["severity"], "🔵")
            print(f"   {icon} {a['message']}")
        if summary["total"] > 5:
            print(f"   … ещё {summary['total'] - 5} — смотрите дашборд.")
    else:
        print("\n🔎 Аномалий не обнаружено.")

    if any_incomplete:
        print("\n⚠️ Часть данных не догрузилась — запуск помечен как неполный. "
              "Подробности в redcat_scraper.log")

    if args.fail_on_critical and counts.get("critical"):
        print("\n❌ Есть критичные аномалии — выход с кодом 2.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
