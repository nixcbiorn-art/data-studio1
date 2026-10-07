"""
Плавающие пороги.
=================

Одна логика: порог растёт с размером объекта, но не выше жёсткого
потолка. Мелкие объекты — MIN, крупные — PCT, огромные — MAX.

    threshold = max(MIN, min(MAX, PCT% × размер))

Пример: разница в 10 лотов на ЖК с 35 лотами — это 29% (шум не
скрывает проблему). Та же разница на ЖК с 5000 — 0.2% (тоже шум,
но уже по другой причине). Плавающий порог масштабируется.

Отдельная функция — для Δ% медиан в мелких группах: там порог
растёт потому, что медиана нестабильна на малой выборке.
"""
from __future__ import annotations


def adaptive(size: int, *,
             min_abs: float = 10.0,
             pct: float = 3.0,
             max_abs: float = 20.0) -> float:
    """Плавающий порог: max(min_abs, min(max_abs, pct × size))."""
    if size <= 0:
        return min_abs
    return max(min_abs, min(max_abs, pct / 100.0 * size))


def rows_threshold(rows_rc: int, rows_src: int) -> int:
    """Порог для сверки числа строк между двумя наборами."""
    base = max(rows_rc, rows_src)
    return int(round(adaptive(base, min_abs=10, pct=3.0, max_abs=20)))


def percent_threshold(size: int, base_pct: float = 2.0,
                      max_multiplier: float = 3.0,
                      scale: float = 50.0) -> float:
    """Порог Δ% для групп: у мелких — выше.

    size      — размер меньшей из двух групп
    base_pct  — базовый порог для крупных групп
    max_multiplier  — во сколько раз порог может вырасти у мелких
    scale     — размер, при котором порог = base_pct

    Формула: base_pct + (base_pct*(max_multiplier-1)) * (1 - size/scale),
    ограничена сверху base_pct*max_multiplier.
    """
    if size <= 0:
        return base_pct * max_multiplier
    if size >= scale:
        return base_pct
    max_pct = base_pct * max_multiplier
    frac = 1 - size / scale
    thr = base_pct + (max_pct - base_pct) * frac
    return max(base_pct, min(max_pct, thr))


def z_threshold_for_group(size: int, base_z: float = 3.5,
                          extra: float = 1.5,
                          scale: float = 30.0) -> float:
    """Порог z-score для групп: у мелких — выше."""
    if size >= scale:
        return base_z
    if size <= 0:
        return base_z + extra
    frac = 1 - size / scale
    return base_z + extra * frac
