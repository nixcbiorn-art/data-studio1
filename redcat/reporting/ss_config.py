"""ss_config — корень проекта, пороги Δ% и кандидаты колонок застройщика."""
from __future__ import annotations

from redcat.core import paths
import sys


BASE = paths.ROOT


sys.path.insert(0, str(BASE))


# ──────────────────────────────────────────────────────────────
#  ПОРОГИ КЛАССОВ ПО |Δ%|
# ──────────────────────────────────────────────────────────────
# Сравнивается модуль расхождения между источником и Redcat:
#   |Δ| ≤ DELTA_OK                    → ok, «сходится»
#   DELTA_OK < |Δ| ≤ DELTA_WARN       → warn, «внимание»
#   |Δ| > DELTA_WARN                  → crit, «смотреть руками»
#
# DELTA_CRIT — отдельный, более грубый порог: используется в гистограмме
# (окрашивание «крайних» столбцов) и в подписи «≥N% у скольких ЖК».
DELTA_OK = 2.0


DELTA_WARN = 10.0


DELTA_CRIT = 50.0


# Колонки, в которых может лежать застройщик. Проверяем по порядку.
_DEVELOPER_COLUMN_CANDIDATES = (
    "developer_name", "developer.name", "developer",
    "Застройщик", "застройщик", "developer_title",
)
