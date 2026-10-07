"""web_crosscheck — отчёт сверки источников."""
from __future__ import annotations

from redcat.data import dataops
from redcat.sources import hc_aliases
from redcat.sources import name_normalizer
from redcat.web.web_config import _pct_thr
from redcat.web.web_data import _aggregate, _normalize_key, db_for_table, load_specs
from redcat.web.web_filters import _apply_virtual_filters, _sanitize_filters


def _cross_check_report(source_key: str) -> dict:
    """Собирает отчёт по сверке одного внешнего источника с таблицей-соседом.

    Возвращает словарь с полями:
      source, with_table, on_left, on_right, metrics, agg,
      summary: {счётчики по классам}
      items_by_class: {класс: [записи]}
    """
    specs = load_specs()
    spec = specs.get(source_key)
    if spec is None:
        raise dataops.DataError(f"Источник «{source_key}» не найден")
    cc = getattr(spec, "cross_check", None) or {}
    if not cc:
        raise dataops.DataError(
            f"У источника «{source_key}» не задана секция cross_check в spec.")

    with_table = cc.get("with_table")
    on_left = cc.get("on_left")
    on_right = cc.get("on_right")
    metrics = cc.get("metrics") or {}
    agg = (cc.get("agg") or "median").lower()
    filter_right = cc.get("filter_right") or None
    filter_left = cc.get("filter_left") or None
    normalize_key = bool(cc.get("normalize_key", True))   # по умолчанию ВКЛ
    min_group_size = int(cc.get("min_group_size", 10))
    thresholds = cc.get("thresholds") or {}
    t_ok = float(thresholds.get("ok", 10))
    t_warn = float(thresholds.get("warn", 20))

    # Приоритет синонимов:
    #   1. spec.key_aliases (перекрывает всё — для специфики источника)
    #   2. hc_aliases.json (глобальный словарь, общий для всех источников)
    #   3. нормализованное имя как есть.
    aliases: dict[str, str] = {}

    # Сначала глобальный словарь
    for k, v in hc_aliases._load().items():
        if k and v:
            aliases[k] = v

    # Затем spec.key_aliases — перекрывает глобальные, если совпадает ключ
    raw_aliases = cc.get("key_aliases") or {}
    for k, v in raw_aliases.items():
        nk = _normalize_key(k) if normalize_key else str(k).strip().lower()
        nv = _normalize_key(v) if normalize_key else str(v).strip().lower()
        if nk and nv:
            aliases[nk] = nv

    if not (with_table and on_left and on_right and metrics):
        raise dataops.DataError(
            "cross_check должен содержать with_table, on_left, on_right "
            "и хотя бы одну метрику в metrics")

    left_db = db_for_table(source_key)
    right_db = db_for_table(with_table)

    # ---- читаем левую выборку ----
    left_cols = [on_left] + list(metrics.keys())
    left_filters = []
    if isinstance(filter_left, list):
        left_filters = [f for f in filter_left if f.get("field")]
    elif filter_left and filter_left.get("field"):
        left_filters = [filter_left]
    left_filters, dropped_left, virtual_left = _sanitize_filters(
        left_db, source_key, left_filters)
    _virtual_left_sql, _virtual_left_args = _apply_virtual_filters(
        left_db, source_key, virtual_left)
    if virtual_left:
        print(f"  ℹ️  {source_key}: виртуальные фильтры "
              f"слева: {[v.get('field') for v in virtual_left]}")
    if dropped_left:
        print(f"  ⚠️  {source_key}: фильтры по несуществующим "
              f"колонкам отброшены: {dropped_left}")
    left_rows = []
    try:
        if _virtual_left_sql:
            from pathlib import Path as _P2
            import sqlite3 as _sq2
            _db2 = _P2(left_db)
            _c2 = _sq2.connect(f"file:{_db2}?mode=ro", uri=True)
            _c2.row_factory = _sq2.Row
            _cols2 = ", ".join(f'"{c}"' for c in left_cols)
            _b2, _a2 = dataops.build_where(
                _c2, source_key, left_filters or [], "AND")
            _w2 = _b2 or ""
            if _w2 and _virtual_left_sql:
                _w2 += " AND (" + _virtual_left_sql + ")"
            elif _virtual_left_sql:
                _w2 = "WHERE (" + _virtual_left_sql + ")"
            _a2 = list(_a2) + list(_virtual_left_args)
            _cur2 = _c2.execute(
                f'SELECT {_cols2} FROM "{source_key}" {_w2}', _a2)
            left_rows = [dict(r) for r in _cur2.fetchall()]
            _c2.close()
        else:
            stream = dataops.iter_all(left_db, source_key,
                                      filters=left_filters, match="AND",
                                      select=left_cols)
            next(stream, None)
            left_rows = list(stream)
    except dataops.DataError as e:
        raise dataops.DataError(f"Левая таблица «{source_key}»: {e}") from e

    # ---- читаем правую выборку (с фильтром) ----
    right_cols = [on_right] + list(metrics.values())
    right_filters = []
    if isinstance(filter_right, list):
        right_filters = [f for f in filter_right if f.get("field")]
    elif filter_right and filter_right.get("field"):
        right_filters = [filter_right]
    right_filters, dropped_right, virtual_right = _sanitize_filters(
        right_db, with_table, right_filters)
    virtual_right_sql, virtual_right_args = _apply_virtual_filters(
        right_db, with_table, virtual_right)
    if dropped_right:
        print(f"  ⚠️  {source_key}: фильтры справа по "
              f"несуществующим колонкам отброшены: {dropped_right}")
    if virtual_right:
        print(f"  ℹ️  {source_key}: фильтры справа через JOIN: "
              f"{[v.get('field') for v in virtual_right]}")
    right_rows = []
    try:
        if virtual_right_sql:
            # Есть виртуальные фильтры — читаем через SQL вручную.
            from pathlib import Path as _P
            import sqlite3 as _sq
            _db = _P(right_db)
            _conn = _sq.connect(f"file:{_db}?mode=ro", uri=True)
            _conn.row_factory = _sq.Row
            _cols_sql = ", ".join(
                f'"{c}"' for c in right_cols)
            _base, _args = dataops.build_where(
                _conn, with_table, right_filters or [], "AND")
            _where = _base or ""
            if _where and virtual_right_sql:
                _where += " AND (" + virtual_right_sql + ")"
            elif virtual_right_sql:
                _where = "WHERE (" + virtual_right_sql + ")"
            _args = list(_args) + list(virtual_right_args)
            _cur = _conn.execute(
                f'SELECT {_cols_sql} FROM "{with_table}" {_where}',
                _args)
            right_rows = [dict(r) for r in _cur.fetchall()]
            _conn.close()
        else:
            stream = dataops.iter_all(right_db, with_table,
                                      filters=right_filters, match="AND",
                                      select=right_cols)
            next(stream, None)
            right_rows = list(stream)
    except dataops.DataError as e:
        raise dataops.DataError(f"Правая таблица «{with_table}»: {e}") from e
    except Exception as e:
        raise dataops.DataError(
            f"Правая таблица «{with_table}»: {type(e).__name__}: {e}"
        ) from e

    # ---- группируем по нормализованному ключу ----
    def _key(row, col):
        raw = row.get(col)
        if raw is None or raw == "":
            return None
        if normalize_key:
            return _normalize_key(raw, aliases)
        return str(raw).strip().lower()

    left_groups: dict[str, list] = {}
    right_groups: dict[str, list] = {}
    left_display: dict[str, str] = {}
    right_display: dict[str, str] = {}

    for r in left_rows:
        k = _key(r, on_left)
        if k is None:
            continue
        left_groups.setdefault(k, []).append(r)
        left_display.setdefault(k, str(r.get(on_left) or k))
    for r in right_rows:
        k = _key(r, on_right)
        if k is None:
            continue
        right_groups.setdefault(k, []).append(r)
        right_display.setdefault(k, str(r.get(on_right) or k))

    # ---- автосинонимы: Скай Гарден ↔ Sky Garden, Эко Бунино ↔ ЭкоБунино ----
    # Только однозначные пары. Ручные aliases (spec + hc_aliases.json) главнее.
    auto_added = 0
    if normalize_key:
        only_l = [k for k in left_groups if k not in right_groups]
        only_r = [k for k in right_groups if k not in left_groups]
        if only_l and only_r:
            found = name_normalizer.auto_aliases(
                only_l, only_r, existing=aliases,
                left_counts={k: len(left_groups[k]) for k in only_l},
                right_counts={k: len(right_groups[k]) for k in only_r})["aliases"]
            for lk, rk in found.items():
                if rk in right_groups and lk in left_groups:
                    _n_l = len(left_groups[lk])
                    _n_r = len(right_groups[rk])
                    print(f"  ⚠️  автосопоставление: "
                          f"{lk!r} ({_n_l}) → {rk!r} ({_n_r})")
                    left_groups.setdefault(rk, []).extend(left_groups.pop(lk))
                    left_display.setdefault(rk, left_display.pop(lk, lk))
                    auto_added += 1

    all_keys = sorted(set(left_groups) | set(right_groups))

    items_by_class: dict[str, list] = {
        "critical": [], "warn": [], "ok": [],
        "left_only": [], "right_only": [], "insufficient": [],
    }

    matched_keys = 0
    for k in all_keys:
        lg = left_groups.get(k, [])
        rg = right_groups.get(k, [])
        display = left_display.get(k) or right_display.get(k) or k

        if not lg and rg:
            items_by_class["right_only"].append({
                "key": k, "display": display,
                "left_rows": 0, "right_rows": len(rg),
            })
            continue
        if lg and not rg:
            items_by_class["left_only"].append({
                "key": k, "display": display,
                "left_rows": len(lg), "right_rows": 0,
            })
            continue

        matched_keys += 1
        if min(len(lg), len(rg)) < min_group_size:
            items_by_class["insufficient"].append({
                "key": k, "display": display,
                "left_rows": len(lg), "right_rows": len(rg),
                "metrics": {},
            })
            continue

        per_metric = {}
        worst = 0.0
        for lf, rf in metrics.items():
            lv, ln = _aggregate([r.get(lf) for r in lg], agg)
            rv, rn = _aggregate([r.get(rf) for r in rg], agg)
            diff_abs = None
            diff_pct = None
            if lv is not None and rv is not None and rv != 0:
                diff_abs = round(lv - rv, 2)
                diff_pct = round((lv - rv) / abs(rv) * 100, 1)
                worst = max(worst, abs(diff_pct))
            per_metric[lf] = {
                "left": lv, "left_n": ln,
                "right": rv, "right_n": rn,
                "diff_abs": diff_abs, "diff_pct": diff_pct,
            }

        _l, _r = len(lg), len(rg)
        _base = max(_l, _r) or 1
        rows_pct = round((_l - _r) / _base * 100, 1)
        item = {
            "key": k, "display": display,
            "left_rows": _l, "right_rows": _r,
            "rows_pct": rows_pct,
            "metrics": per_metric,
            "worst_pct": worst,
        }
        # Асимметричное правило (для А101): если в спеке стоит
        # asymmetric = "source_higher", то положительное отклонение
        # (источник > Redcat) — ожидаемое, у нас есть вторая скидка,
        # которой нет в фиде. Считаем только отрицательные отклонения.
        _asym_filter = (cc.get("asymmetric") == "source_higher")
        _effective_worst = worst
        # Redcat — архив, источник — активное. Если у Redcat больше
        # лотов и в спеке asymmetric_rows="redcat_higher" — не считать
        # расхождение проблемой (разница объясняется проданными лотами).
        _asym_rows = cc.get("asymmetric_rows")
        if _asym_rows == "redcat_higher" and len(lg) > len(rg):
            _effective_worst = 0.0
        if _asym_filter:
            # Берём модуль только отрицательных отклонений.
            _neg = [abs(m.get("diff_pct") or 0)
                    for m in per_metric.values()
                    if (m.get("diff_pct") or 0) < 0]
            _effective_worst = max(_neg) if _neg else 0.0

        # Плавающие пороги: для маленьких групп медиана
        # нестабильна, порог классификации поднимаем.
        _size = min(len(lg), len(rg))
        t_ok_eff = max(t_ok, _pct_thr(_size, base_pct=t_ok,
                                       max_multiplier=3.0,
                                       scale=50.0))
        t_warn_eff = max(t_warn, _pct_thr(_size, base_pct=t_warn,
                                           max_multiplier=3.0,
                                           scale=50.0))
        if _effective_worst >= t_warn_eff:
            items_by_class["critical"].append(item)
        elif _effective_worst >= t_ok_eff:
            items_by_class["warn"].append(item)
        else:
            items_by_class["ok"].append(item)

    for cls in ("critical", "warn"):
        items_by_class[cls].sort(key=lambda x: -x.get("worst_pct", 0))

    summary = {
        "total_keys": len(all_keys),
        "matched": matched_keys,
        "critical": len(items_by_class["critical"]),
        "warn": len(items_by_class["warn"]),
        "ok": len(items_by_class["ok"]),
        "insufficient": len(items_by_class["insufficient"]),
        "left_only": len(items_by_class["left_only"]),
        "right_only": len(items_by_class["right_only"]),
    }

    return {
        "source": source_key,
        "with_table": with_table,
        "on_left": on_left,
        "on_right": on_right,
        "metrics": metrics,
        "agg": agg,
        "normalize_key": normalize_key,
        "min_group_size": min_group_size,
        "thresholds": {"ok": t_ok, "warn": t_warn},
        "filter_right": filter_right,
        "left_label": spec.title or source_key,
        "right_label": ((specs.get(with_table).title
                         if specs.get(with_table) and specs.get(with_table).title
                         else with_table)),
        "left_external": bool(spec.external),
        "right_external": bool(specs.get(with_table).external)
                          if specs.get(with_table) else False,
        "aliases_total": len(aliases),
        "auto_aliases": auto_added,
        "summary": summary,
        "items_by_class": {k: v[:500] for k, v in items_by_class.items()},
    }
