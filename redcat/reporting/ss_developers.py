"""ss_developers — определение застройщиков и агрегация по ним."""
from __future__ import annotations

import sqlite3
import statistics
from redcat.web import webapp
from redcat.reporting.ss_config import _DEVELOPER_COLUMN_CANDIDATES


# ──────────────────────────────────────────────────────────────
#  СВОРАЧИВАНИЕ ПО ЗАСТРОЙЩИКАМ
# ──────────────────────────────────────────────────────────────
def _aliases_for(report: dict) -> dict:
    """Собирает словарь алиасов так же, как webapp._cross_check_report:
    глобальный hc_aliases.json + key_aliases из spec ИСХОДНОГО источника
    (после swap_report он лежит в report["with_table"]).
    """
    aliases: dict = {}
    try:
        from redcat.sources import hc_aliases
        for k, v in hc_aliases._load().items():
            if k and v:
                aliases[k] = v
    except Exception:
        pass
    spec = webapp.load_specs().get(report.get("with_table"))
    cc = getattr(spec, "cross_check", None) or {}
    for k, v in (cc.get("key_aliases") or {}).items():
        nk = webapp._normalize_key(k)
        nv = webapp._normalize_key(v)
        if nk and nv:
            aliases[nk] = nv
    return aliases


def _pick_developer_column(cols: set) -> str | None:
    """Ищет первую подходящую колонку-застройщика среди candidates."""
    for name in _DEVELOPER_COLUMN_CANDIDATES:
        if name in cols:
            return name
    return None


def _developer_map_for_table(db, table: str, name_field: str,
                             aliases: dict, normalize_key: bool) -> dict:
    """{нормализованный_ключ_ЖК: застройщик} из одной таблицы.

    Сначала пробует взять developer_name прямо из таблицы. Если такого
    поля нет (как в apartments Redcat), но есть housing_complex_id,
    подтягивает застройщика через связь с housing_complexes.
    """
    if not table or not name_field:
        return {}
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            if name_field not in cols:
                return {}
            dev_col = _pick_developer_column(cols)
            rows = []
            if dev_col:
                rows = conn.execute(
                    f'SELECT DISTINCT "{name_field}" AS nm, "{dev_col}" AS dev '
                    f'FROM "{table}" '
                    f'WHERE "{name_field}" IS NOT NULL AND "{dev_col}" IS NOT NULL'
                ).fetchall()
            if not rows and "housing_complex_id" in cols:
                hc_dev_col = None
                hc_id_col = None
                hc_table = None
                for ht in ("housing_complexes", "housing_complex",
                           "hc", "complexes"):
                    try:
                        hcols = {r[1] for r in conn.execute(
                            f'PRAGMA table_info("{ht}")')}
                    except sqlite3.Error:
                        continue
                    if not hcols:
                        continue
                    hc_dev_col = next(
                        (c for c in ("developer_name", "developer.name",
                                     "developer", "Застройщик")
                         if c in hcols), None)
                    hc_id_col = "id" if "id" in hcols else None
                    if hc_dev_col and hc_id_col:
                        hc_table = ht
                        break
                    hc_dev_col = None
                if hc_dev_col and hc_id_col and hc_table:
                    rows = conn.execute(
                        f'SELECT DISTINCT t."{name_field}" AS nm, '
                        f'h."{hc_dev_col}" AS dev '
                        f'FROM "{table}" t '
                        f'JOIN "{hc_table}" h ON '
                        f'CAST(h."{hc_id_col}" AS TEXT) = '
                        f'CAST(t."housing_complex_id" AS TEXT) '
                        f'WHERE t."{name_field}" IS NOT NULL '
                        f'AND h."{hc_dev_col}" IS NOT NULL'
                    ).fetchall()
    except sqlite3.Error:
        return {}

    out: dict = {}
    for r in rows:
        nm = r["nm"] if hasattr(r, "keys") else r[0]
        dev = r["dev"] if hasattr(r, "keys") else r[1]
        if not dev:
            continue
        k = (webapp._normalize_key(nm, aliases) if normalize_key
             else str(nm).strip().lower())
        if k and k not in out:
            out[k] = dev
    return out


def _developer_maps(report: dict) -> dict:
    """{нормализованный_ключ_ЖК: застройщик} — с ОБЕИХ сторон сверки."""
    aliases = _aliases_for(report)
    normalize_key = bool(report.get("normalize_key", True))
    out: dict = {}

    # 1. внешний источник (низкий приоритет)
    ext_table = report.get("with_table")
    ext_field = report.get("on_right")
    if ext_table and ext_field:
        try:
            ext_db = webapp.db_for_table(ext_table)
        except Exception:
            ext_db = webapp.EXTERNAL_DB
        for k, dev in _developer_map_for_table(
                ext_db, ext_table, ext_field, aliases, normalize_key).items():
            out[k] = dev

    # 2. Redcat (высокий приоритет — перекрывает внешний)
    rc_table = report.get("source")
    rc_field = report.get("on_left")
    if rc_table and rc_field:
        try:
            rc_db = webapp.db_for_table(rc_table)
        except Exception:
            rc_db = webapp.DATA_DB
        for k, dev in _developer_map_for_table(
                rc_db, rc_table, rc_field, aliases, normalize_key).items():
            out[k] = dev

    return out


def _attach_developers(report: dict) -> None:
    """Проставляет каждому ЖК в отчёте имя застройщика (in place)."""
    dev_map = _developer_maps(report)
    if not dev_map:
        return
    for cls in ("critical", "warn", "ok", "insufficient",
                "left_only", "right_only"):
        for item in report["items_by_class"].get(cls) or []:
            dev = dev_map.get(item.get("key"))
            if dev:
                item["developer"] = dev


def aggregate_by_developer(report: dict) -> list:
    """Сворачивает ЖК по застройщику."""
    metric_names = list((report.get("metrics") or {}).keys())
    groups: dict = {}

    classes = ("critical", "warn", "ok", "insufficient",
               "left_only", "right_only")
    for cls in classes:
        for item in report["items_by_class"].get(cls) or []:
            dev = item.get("developer") or "(застройщик неизвестен)"
            g = groups.setdefault(dev, {
                "developer": dev, "jc_count": 0,
                "critical": 0, "warn": 0, "ok": 0, "insufficient": 0,
                "left_only": 0, "right_only": 0,
                "left_rows": 0, "right_rows": 0, "worst_pct": 0.0,
                "_pcts": {m: [] for m in metric_names},
                "items": [],
            })
            g["jc_count"] += 1
            g[cls] += 1
            g["left_rows"] += item.get("left_rows", 0) or 0
            g["right_rows"] += item.get("right_rows", 0) or 0
            g["worst_pct"] = max(g["worst_pct"], item.get("worst_pct", 0) or 0)
            g["items"].append(item)
            for m in metric_names:
                mm = (item.get("metrics") or {}).get(m)
                if mm and mm.get("diff_pct") is not None:
                    g["_pcts"][m].append(mm["diff_pct"])

    out = []
    for g in groups.values():
        g["metrics"] = {
            m: {"median_pct": round(statistics.median(pcts), 2),
                "max_abs_pct": round(max(abs(p) for p in pcts), 2),
                "n": len(pcts)}
            for m, pcts in g["_pcts"].items() if pcts
        }
        del g["_pcts"]
        out.append(g)

    out.sort(key=lambda x: (-x["critical"], -x["warn"],
                            -x["worst_pct"], -x["jc_count"]))
    return out
