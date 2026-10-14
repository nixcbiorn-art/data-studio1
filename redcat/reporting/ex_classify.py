"""ex_classify — классификация ЖК: проблемный или нет."""
from __future__ import annotations

from redcat.reporting.ex_config import AREA_NOISE_ABS, AREA_NOISE_PCT, DISCOUNT_MATCH_MIN, PRICE_NOISE_ABS, ROWS_NOISE_ABS, ROWS_NOISE_PCT, ROWS_STABLE
from redcat.reporting.ex_format import _class_for_pct
from redcat.reporting.ex_prices import _find_area_diff, _find_area_diff_abs, _find_price_diff, _find_price_diff_abs


def classify(item, rows_tol, area_tol, price_threshold, spec=None):
    """Возвращает (kind, cls).

    kind:
      "problem"  — идёт в основной отчёт
      "discount" — в отдельный файл скидок
      None       — ничего интересного
    """
    rows_rc = item.get("rows_rc") or 0
    rows_src = item.get("rows_src") or 0
    rows_diff = abs(rows_rc - rows_src)
    rows_pct = (rows_diff / max(rows_src, 1)) * 100 if rows_src else 0.0

    area_pct = _find_area_diff(item)
    price_pct = _find_price_diff(item)

    rows_bad = (rows_diff > rows_tol
                and rows_diff >= ROWS_NOISE_ABS
                and rows_pct >= ROWS_NOISE_PCT)

    area_abs = _find_area_diff_abs(item)
    price_abs = _find_price_diff_abs(item)

    area_thr = max(area_tol, AREA_NOISE_PCT)
    area_bad = (area_pct is not None
                and abs(area_pct) > area_thr
                and (area_abs is None or abs(area_abs) >= AREA_NOISE_ABS))

    price_bad = (price_pct is not None
                 and abs(price_pct) >= price_threshold
                 and (price_abs is None or abs(price_abs) >= PRICE_NOISE_ABS))

    # Медиана площади нестабильна на границе категорий (студии/1к/2к/3к).
    # Сдвиг 1-2 лотов даёт сдвиг медианы в разы. Если состав и цена
    # совпадают — не считаем это проблемой.
    _rows_stable = rows_pct <= ROWS_STABLE
    _price_stable = price_pct is None or abs(price_pct) <= 2.0
    if _rows_stable and _price_stable:
        area_bad = False

    cc = (getattr(spec, "cross_check", None) or {}) if spec else {}
    asym = cc.get("asymmetric")

    if asym == "source_higher" and price_pct is not None and price_pct > 0:
        price_bad = False

    # Redcat — архив, MR — только активное. Если у Redcat больше лотов
    # и в спеке помечено asymmetric_rows="redcat_higher", не считать
    # это проблемой: разница объясняется проданными лотами.
    # asym_rows убрано

    if not (rows_bad or area_bad or price_bad):
        return None, None

    worst = 0.0
    for v in (price_pct, area_pct):
        if v is not None and abs(v) > worst:
            worst = abs(v)
    if rows_bad:
        worst = max(worst, rows_pct)
    cls = _class_for_pct(worst, price_threshold)

    if asym == "source_higher" and not rows_bad and not area_bad:
        if price_pct is not None and price_pct >= 0:
            return None, None

    if rows_bad or area_bad:
        return "problem", cls

    matches = item.get("price_matches") or {}
    for info in matches.values():
        if info["ratio"] >= DISCOUNT_MATCH_MIN:
            return "discount", cls
    return "problem", cls
