"""ex_diagnose — текстовые диагнозы по расхождению метрик ЖК."""
from __future__ import annotations

from redcat.reporting.ex_config import AREA_NOISE_ABS, AREA_NOISE_PCT, MATCH_FEW, MATCH_MOST, MATCH_NEAR_FULL, NOISE_PCT, PRICE_NOISE_ABS, ROWS_NOISE_ABS, ROWS_NOISE_PCT, ROWS_STABLE
from redcat.reporting.ex_mapping import _is_price
from redcat.reporting.ex_prices import _find_area_diff, _find_area_diff_abs, _find_price_diff, _find_price_diff_abs
from redcat.reporting.ex_texts import DIAG, DIAG_DISC


# ── диагностика одного ЖК ─────────────────────────────────────
def diagnose(item: dict, spec=None) -> list:
    """Список фраз — что именно странно в этом ЖК.

    Пишет только значимые фразы. Шум (2 лота, 0.1% площади) не
    упоминается.
    """
    out = []
    asym = ((getattr(spec, "cross_check", None) or {})
            .get("asymmetric") if spec else None)

    # ── объём ──
    rows_rc = item.get("rows_rc") or 0
    rows_src = item.get("rows_src") or 0
    if rows_rc != rows_src:
        n = abs(rows_rc - rows_src)
        pct = (n / max(rows_src, 1)) * 100 if rows_src else 0.0
        big = pct >= ROWS_NOISE_PCT and n >= ROWS_NOISE_ABS
        if big:
            key = "rows_less_big" if rows_rc < rows_src else "rows_more_big"
            out.append(DIAG[key].format(n=n, pct=pct))

    # ── площадь ──
    # Медиана площади нестабильна на границе категорий. Если состав
    # и цена совпадают — молчим про площадь.
    area_pct = _find_area_diff(item)
    price_pct_now = _find_price_diff(item)
    _rows_stable = (
        (max(rows_rc, rows_src) > 0) and
        (abs(rows_rc - rows_src) / max(rows_src, 1) * 100) <= ROWS_STABLE
    )
    _price_stable = (price_pct_now is None
                     or abs(price_pct_now) <= 2.0)
    area_abs = _find_area_diff_abs(item)
    if (area_pct is not None
            and abs(area_pct) >= AREA_NOISE_PCT
            and (area_abs is None or abs(area_abs) >= AREA_NOISE_ABS)
            and not (_rows_stable and _price_stable)):
        out.append(DIAG["area_diff_big"].format(pct=area_pct))

    # ── цена ──
    price_pct = _find_price_diff(item)
    matches = item.get("price_matches") or {}

    if asym == "source_higher":
        if price_pct is not None and price_pct < -NOISE_PCT:
            out.append(DIAG["price_below"].format(pct=price_pct))
        return out

    # Если даже % большой, но абсолютная разница меньше
    # 50 тыс ₽ — это округление, а не проблема.
    price_abs_now = _find_price_diff_abs(item)
    if (price_pct is not None and abs(price_pct) <= NOISE_PCT):
        return out
    if (price_abs_now is not None
            and abs(price_abs_now) < PRICE_NOISE_ABS):
        return out

    if not matches:
        if any(_is_price(m["metric_left"]) for m in item["metrics"]):
            out.append(DIAG["price_nodata"])
        return out

    for _lf, info in matches.items():
        ratio = info["ratio"]
        matched = info["matched"]
        total = info["total"]
        diff = total - matched
        share = diff / total * 100 if total else 0.0

        if matched == 0:
            out.append(DIAG["price_none"].format(total=total))
        elif ratio < MATCH_FEW:
            out.append(DIAG["price_tiny"].format(
                matched=matched, total=total,
                share=matched / total * 100))
        elif ratio < MATCH_MOST:
            out.append(DIAG["price_most"].format(
                diff=diff, total=total, share=share))
        elif ratio < MATCH_NEAR_FULL:
            out.append(DIAG["price_part"].format(
                diff=diff, total=total, share=share))
        elif ratio < 1.0:
            out.append(DIAG["price_some"].format(
                diff=diff, total=total, share=share))
    return out


def diagnose_discount(item: dict) -> list[str]:
    out = []
    matches = item.get("price_matches") or {}
    price_pct = _find_price_diff(item) or 0.0
    for _lf, info in matches.items():
        ratio = info["ratio"]
        matched = info["matched"]
        total = info["total"]
        diff = total - matched
        share = diff / total * 100 if total else 0.0
        if ratio >= 0.999:
            out.append(DIAG_DISC["price_full"].format(
                total=total, pct=price_pct))
        elif ratio >= MATCH_NEAR_FULL:
            out.append(DIAG_DISC["price_high"].format(
                diff=diff, total=total, share=share, matched=matched))
        elif ratio >= MATCH_MOST:
            out.append(DIAG_DISC["price_mid"].format(
                diff=diff, total=total, share=share))
        else:
            out.append(DIAG_DISC["price_low"].format(
                diff=diff, total=total, share=share))
    return out
