"""
Здоровье фидов (детектор «замороженный/пропавший источник»)
===========================================================
Инциденты вида «etalon три дня подряд отдаёт ровно те же записи, а в
витрине цена +13%» и «profitbase_eon вообще перестал собираться» раньше
были видны только при ручном разборе сравнений. Здесь они находятся сами
по двум признакам:

  • ПРОПАЛ: источник раньше присутствовал в запусках (колонка
    <key>_count не пустая), а в последних N запусках подряд — пусто
    (не собран / пропущен / упал до сохранения).
  • ЗАМОРОЖЕН: <key>_count не меняется K запусков подряд, при этом в
    тех же запусках есть движение (changes_total > 0 или счётчики других
    источников сдвинулись) — то есть весь конвейер жив, а этот фид нет.

Данные берутся только из history запусков (reports/run_stats.db) — ничего
не читается из сети, модуль можно гонять хоть каждый день.

    python -m redcat.quality.feed_health                 # отчёт в консоль
    python -m redcat.quality.feed_health --json          # + reports/feed_health.json
"""
from __future__ import annotations

from redcat.core import paths
import argparse
import json

MISSING_RUNS = 2      # сколько запусков подряд без источника = «пропал»
FROZEN_RUNS = 3       # сколько запусков подряд с одинаковым count = «залип»

STATE_OK, STATE_MISSING, STATE_FROZEN = "ok", "missing", "frozen"


def _run_keys(runs_history) -> list[str]:
    """Все источники, у которых в истории есть колонка <key>_count."""
    keys = []
    for row in runs_history:
        for col in row:
            if col.endswith("_count"):
                k = col[: -len("_count")]
                if k and k not in keys:
                    keys.append(k)
    return keys


def _zero_changes_frozen(runs_history, key, frozen_runs):
    """Второй вкус заморозки: содержимое фида не меняется (изменений 0)
    несколько запусков подряд, хотя раньше менялось. Число изменений по
    источнику пишет скрапер (колонка <key>_changes); случай etalon —
    ровно он: объём постоянный, цены во фиде стоят, витрина уходит.
    Возвращает строку-объяснение или None."""
    col = f"{key}_changes"
    series = [r.get(col) for r in runs_history if col in r]
    present = [v for v in series if v is not None]
    if len(present) < frozen_runs + 1:
        return None
    tail, earlier = present[-frozen_runs:], present[:-frozen_runs]
    if all(v == 0 for v in tail) and any(v > 0 for v in earlier):
        return (f"содержимое фида не меняется {frozen_runs} запусков подряд "
                f"(изменений 0), раньше менялось — источник залип")
    return None


def check(runs_history, external_only=True, specs=None,
          missing_runs=MISSING_RUNS, frozen_runs=FROZEN_RUNS) -> list[dict]:
    """Возвращает список состояний по каждому источнику в истории."""
    if not runs_history:
        return []
    keys = _run_keys(runs_history)
    if external_only and specs:
        keys = [k for k in keys
                if specs.get(k) is None or getattr(specs[k], "external", False)]

    last = runs_history[-1]
    total_changes_last = last.get("changes_total") or 0
    out = []
    for key in keys:
        col = f"{key}_count"
        series = [(r.get(col) if col in r else None) for r in runs_history]
        present = [v for v in series if v is not None]
        status, detail = STATE_OK, ""

        # ── пропал: хвост из None при том, что раньше источник был ──
        tail_missing = 0
        for v in reversed(series):
            if v is None:
                tail_missing += 1
            else:
                break
        if present and tail_missing >= missing_runs:
            status = STATE_MISSING
            detail = (f"не собран {tail_missing} запуск(ов) подряд "
                      f"(последний объём {present[-1]})")

        # ── заморожен: хвост из одинаковых count при живом конвейере ──
        # Обязательное условие: ДО залипания объём менялся. Статичные
        # справочники (списки ЖК, тарифы) не меняются всегда — это норма,
        # а не симптом.
        elif len(present) >= frozen_runs:
            tail = present[-frozen_runs:]
            earlier = present[:-frozen_runs]
            if (len(set(tail)) == 1 and len(set(earlier)) > 1):
                others_moved = any(
                    len({r.get(f'{k2}_count') for r in runs_history[-frozen_runs:]
                         if r.get(f'{k2}_count') is not None}) > 1
                    for k2 in keys if k2 != key)
                if total_changes_last or others_moved:
                    status = STATE_FROZEN
                    detail = (f"объём {tail[0]} не меняется {frozen_runs} "
                              f"запуска подряд, при этом сверка меняется — "
                              f"похоже, фид отдаёт кэш")
            elif _zero_changes_frozen(runs_history, key, frozen_runs):
                status = STATE_FROZEN
                detail = _zero_changes_frozen(runs_history, key, frozen_runs)
        out.append({"source": key, "state": status, "detail": detail,
                    "last_count": present[-1] if present else None,
                    "tail_missing": tail_missing})
    order = {STATE_MISSING: 0, STATE_FROZEN: 1, STATE_OK: 2}
    out.sort(key=lambda d: order[d["state"]])
    return out


def format_report(states) -> str:
    lines = []
    bad = [s for s in states if s["state"] != STATE_OK]
    if not bad:
        return "📡 Здоровье фидов: замечаний нет."
    for s in bad:
        icon = "🔴" if s["state"] == STATE_MISSING else "🟠"
        label = "ПРОПАЛ" if s["state"] == STATE_MISSING else "ЗАМОРОЖЕН"
        lines.append(f"{icon} [{label}] {s['source']}: {s['detail']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    from redcat.core import safe_stdout
    safe_stdout.enable()
    ap = argparse.ArgumentParser(description="Здоровье внешних фидов")
    from redcat.collection.scrape_config import STATS_DB
    ap.add_argument("--stats-db", default=str(STATS_DB),
                    help="база истории запусков (по умолчанию history/run_stats.db)")
    ap.add_argument("--runs", type=int, default=12,
                    help="сколько последних запусков смотреть")
    ap.add_argument("--internal", action="store_true",
                    help="проверять и внутренние источники (по умолчанию только внешние)")
    ap.add_argument("--json", action="store_true",
                    help=" additionally написать reports/feed_health.json")
    args = ap.parse_args(argv)

    from redcat.data import run_stats
    from redcat.sources import registry as src
    src.load_from_dir(paths.SOURCES_DIR)
    src.load_from_dir(paths.SOURCES_EXT_DIR)
    specs = {s.key: s for s in src.all_sources()}
    history = run_stats.load_history(args.stats_db, limit=args.runs)
    if not history:
        print(f"История запусков пуста ({args.stats_db}) — сказать нечего.")
        return 1
    states = check(history, external_only=not args.internal, specs=specs)
    print(format_report(states))
    ok = sum(1 for s in states if s["state"] == STATE_OK)
    print(f"Итого: {ok} ok, "
          f"{sum(1 for s in states if s['state'] == 'missing')} пропало, "
          f"{sum(1 for s in states if s['state'] == 'frozen')} залипло.")
    if args.json:
        out = paths.REPORTS_DIR / "feed_health.json"
        out.parent.mkdir(exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"runs_examined": len(history), "states": states},
                      f, ensure_ascii=False, indent=1)
        print(f"Записан {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
