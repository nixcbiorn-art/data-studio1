"""ex_prices — карты цен и поиск расхождений по цене и площади."""
from __future__ import annotations

from collections import Counter
from redcat.data import dataops
from redcat.sources import hc_aliases
from redcat.web import webapp
from redcat.reporting.ex_format import _to_num
from redcat.reporting.ex_mapping import _is_price, _price_pairs


# ── распределение цен по лотам ────────────────────────────────
def _aliases_for(source_key: str, specs: dict) -> dict:
    aliases: dict = {}
    try:
        for k, v in hc_aliases._load().items():
            if k and v:
                aliases[k] = v
    except Exception:
        pass
    spec = specs.get(source_key)
    cc = getattr(spec, "cross_check", None) or {}
    for k, v in (cc.get("key_aliases") or {}).items():
        nk = webapp._normalize_key(k)
        nv = webapp._normalize_key(v)
        if nk and nv:
            aliases[nk] = nv
    return aliases


def _build_price_map(source_key: str, specs: dict) -> dict:
    """Собирает распределения цен по лотам для сопоставления.

    ВАЖНО: применяет filter_right так же, как webapp._cross_check_report.
    Без этого в сопоставление цен попадали машино-места, кладовки и
    чужие ЖК — отсюда ложные «ни одна цена не совпала».
    """
    spec = specs.get(source_key)
    cc = getattr(spec, "cross_check", None) or {}
    if not cc:
        return {}
    on_left = cc.get("on_left")
    on_right = cc.get("on_right")
    right_table = cc.get("with_table")
    metrics = cc.get("metrics") or {}
    filter_right = cc.get("filter_right") or None
    if not (on_left and on_right and right_table and metrics):
        return {}

    price_pairs = [(lf, rf) for lf, rf, _, _ in _price_pairs(metrics)]
    if not price_pairs:
        return {}

    normalize_key = bool(cc.get("normalize_key", True))
    aliases = _aliases_for(source_key, specs)

    def _key(raw):
        if raw in (None, ""):
            return None
        if normalize_key:
            return webapp._normalize_key(raw, aliases)
        return str(raw).strip().lower()

    left_fields = list({lf for lf, _ in price_pairs})
    right_fields = list({rf for _, rf in price_pairs})

    left_db = webapp.db_for_table(source_key)
    right_db = webapp.db_for_table(right_table)

    out: dict = {}

    try:
        stream = dataops.iter_all(left_db, source_key,
                                  select=list({on_left} | set(left_fields)))
        next(stream, None)
        for row in stream:
            k = _key(row.get(on_left))
            if not k:
                continue
            entry = out.setdefault(k, {})
            for lf in left_fields:
                v = _to_num(row.get(lf))
                if v is None:
                    continue
                slot = entry.setdefault(lf, {"left": Counter(), "right": Counter()})
                slot["left"][v] += 1
    except Exception as e:
        print(f"  ⚠️  {source_key}.{on_left}: {type(e).__name__}: {e}")

    right_filters = []
    if isinstance(filter_right, list):
        right_filters = [f for f in filter_right if f.get("field")]
    elif filter_right and filter_right.get("field"):
        right_filters = [filter_right]
    right_filters, dropped, virtual = webapp._sanitize_filters(
        right_db, right_table, right_filters)
    if dropped:
        print(f"  ⚠️  {source_key}: отброшены фильтры справа: {dropped}")

    try:
        if virtual:
            import sqlite3 as _sq
            _conn = _sq.connect(f"file:{right_db}?mode=ro", uri=True)
            _conn.row_factory = _sq.Row
            try:
                _cols_sql = ", ".join(
                    f'"{c}"' for c in ({on_right} | set(right_fields)))
                _base, _args = dataops.build_where(
                    _conn, right_table, right_filters or [], "AND")
                _v_sql, _v_args = webapp._apply_virtual_filters(
                    right_db, right_table, virtual)
                _where = _base or ""
                if _where and _v_sql:
                    _where += " AND (" + _v_sql + ")"
                elif _v_sql:
                    _where = "WHERE (" + _v_sql + ")"
                _args = list(_args) + list(_v_args)
                for row in _conn.execute(
                        f'SELECT {_cols_sql} FROM "{right_table}" {_where}',
                        _args):
                    row = dict(row)
                    k = _key(row.get(on_right))
                    if not k:
                        continue
                    entry = out.setdefault(k, {})
                    for lf, rf in price_pairs:
                        v = _to_num(row.get(rf))
                        if v is None:
                            continue
                        slot = entry.setdefault(
                            lf, {"left": Counter(), "right": Counter()})
                        slot["right"][v] += 1
            finally:
                _conn.close()
        else:
            stream = dataops.iter_all(right_db, right_table,
                                      filters=right_filters, match="AND",
                                      select=list({on_right} | set(right_fields)))
            next(stream, None)
            for row in stream:
                k = _key(row.get(on_right))
                if not k:
                    continue
                entry = out.setdefault(k, {})
                for lf, rf in price_pairs:
                    v = _to_num(row.get(rf))
                    if v is None:
                        continue
                    slot = entry.setdefault(
                        lf, {"left": Counter(), "right": Counter()})
                    slot["right"][v] += 1
    except Exception as e:
        print(f"  ⚠️  {right_table}.{on_right}: {type(e).__name__}: {e}")

    return out


def _match_ratio(left_dist: Counter, right_dist: Counter):
    if not left_dist or not right_dist:
        return None, 0, 0
    common = set(left_dist) & set(right_dist)
    matched = sum(min(left_dist[p], right_dist[p]) for p in common)
    total = max(sum(left_dist.values()), sum(right_dist.values()))
    if total == 0:
        return None, 0, 0
    return matched / total, matched, total


# ── поиск расхождений ─────────────────────────────────────────
def _find_area_diff(item):
    out = None
    for m in item["metrics"]:
        ln = m["metric_left"].lower()
        rn = m["metric_right"].lower()
        if "area" in ln or "area" in rn or "площад" in ln or "площад" in rn:
            pct = m["diff_pct"]
            if pct is None:
                continue
            if out is None or abs(pct) > abs(out):
                out = pct
    return out


def _find_price_diff(item):
    out = None
    for m in item["metrics"]:
        ln = m["metric_left"].lower()
        rn = m["metric_right"].lower()
        if _is_price(ln) or _is_price(rn):
            pct = m["diff_pct"]
            if pct is None:
                continue
            if out is None or abs(pct) > abs(out):
                out = pct
    return out


def _find_price_diff_abs(item):
    """Абсолютная разница по цене (в рублях) — максимум по модулю."""
    out = None
    for m in item["metrics"]:
        ln = m["metric_left"].lower()
        rn = m["metric_right"].lower()
        if _is_price(ln) or _is_price(rn):
            d = m.get("diff_abs")
            if d is None:
                continue
            if out is None or abs(d) > abs(out):
                out = d
    return out


def _find_area_diff_abs(item):
    """Абсолютная разница по площади (в м²) — максимум по модулю."""
    out = None
    for m in item["metrics"]:
        ln = m["metric_left"].lower()
        rn = m["metric_right"].lower()
        if "area" in ln or "area" in rn or "площад" in ln or "площад" in rn:
            d = m.get("diff_abs")
            if d is None:
                continue
            if out is None or abs(d) > abs(out):
                out = d
    return out
