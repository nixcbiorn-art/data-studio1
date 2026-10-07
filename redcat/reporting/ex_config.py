"""ex_config — пороги шума и классов, корень проекта и сопоставление источников с застройщиками."""
from __future__ import annotations

from redcat.core import paths
import sys


try:
    from redcat.data.thresholds import rows_threshold as _rows_threshold
except ImportError:
    def _rows_threshold(a, b):
        return 10


NOISE_PCT = 2.0   # |Δ медианы| ниже — считаем шумом


# Шумовые пороги: ниже этих значений — не проблема.
ROWS_NOISE_ABS = 10      # меньше 3 лотов разницы — шум


ROWS_NOISE_PCT = 3.0    # меньше 1.5% разницы — шум


ROWS_STABLE = 5.0  # если Δ строк < 5% и Δ цены < 2% — медиана площади нестабильна


# Минимальные абсолютные пороги: ниже этих значений — округление,
# а не проблема. Даже если % большой — разница в 5000 ₽ не поднимет
# тревогу на цене 14 млн ₽.
PRICE_NOISE_ABS = 50000.0   # 50 тыс ₽


AREA_NOISE_ABS = 0.3        # 0.3 м²


AREA_NOISE_PCT = 2.0    # меньше 2% площади — шум


HERE = paths.ROOT


sys.path.insert(0, str(HERE))


SOURCE_TO_DEVELOPER = {
    "jcat_lsr_apartments_normalized": "ЛСР",
    "samolet_apartments":             "Самолёт",
    "fsk_apartments":                 "ФСК/ДСК",
    "a101_apartments":                "А101",
    "rbi_apartments":                 "RBI",
    "dsk_apartments":                 "ДСК",
}


# Пороги значимости: ниже — не считаем сигналом (шум/округление).
ROWS_PCT_NOISE = 2.0    # % расхождения объёма


ROWS_ABS_NOISE = 3      # лотов, минимальная разница


AREA_PCT_NOISE = 1.0    # % расхождения площади


# Границы для доли совпадения цен.
MATCH_NEAR_FULL = 0.9


MATCH_MOST      = 0.5


MATCH_FEW       = 0.1


# Порог доли совпадения цен, ниже которого расхождение цены считается
# проблемой, а не скидкой.
DISCOUNT_MATCH_MIN = 0.5


# ── вид цены по имени ─────────────────────────────────────────
_DISCOUNTED = ("discount", "sale", "promo", "act", "акци", "скидк")


_BASE = ("base", "list", "regular", "catalog", "каталож", "базов", "без_скидки")
