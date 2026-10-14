"""
Дедупликация записей по id_field
================================
Бывают фиды, где один и тот же external_id приходит дважды в одном
прогоне (например, а101 — 10 дублей в сутки). Такие повторы:
  • раздувают объём фида и занижают покрытие сверки;
  • затирают друг друга в snapshot-сравнении (кто последний — тот и
    «истина»), из-за чего изменения полей не видны;
  • ломают record_history (одной записи приписываются два значения).

Правило: среди строк с одинаковым id оставляем одну — «самую свежую» по
первому попавшемуся временнóму полю (last_update_date и т.п.), иначе
последнюю по порядку фида. Строки без id не трогаем.

    from redcat.collection.scrape_dedup import dedup_rows
    rows, removed, examples = dedup_rows(rows, spec)
"""
from __future__ import annotations

import re

# Поля-кандидаты «даты изменения» в порядке предпочтения; значения —
# строки дат, сортируемые лексикографически (ISO и DD.MM.YYYY-оба хуже,
# поэтому сравниваем как есть: кто строкой «больше», тот новее для ISO).
TS_FIELDS = ("last_update_date", "last_update", "updated_at", "updatedAt",
             "modification_date", "modificationts", "change_date",
             "creation_date", "created_at", "published_at", "date", "timestamp")

_NULLISH = ("", "nan", "none", "null")


def row_id(row, id_field):
    """Идентификатор строки или None. 535 / '535' / '535.0' — одно и то же."""
    v = row.get(id_field)
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in _NULLISH:
        return None
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def _best_idx(rows, idxs):
    """Из индексов-дублей выбрать «самую свежую» запись."""
    for tf in TS_FIELDS:
        vals = [(i, str(rows[i].get(tf) or "")) for i in idxs]
        if any(v for _, v in vals):
            return max(vals, key=lambda kv: kv[1])[0]
    return idxs[-1]  # нет времени — берём последнего в фиде (как было и раньше)


def dedup_rows(rows, spec):
    """Возвращает (дедуплированные строки, сколько убрали, примеры id).

    Порядок сохранения: позиция первого вхождения id в исходном списке.
    """
    id_field = getattr(spec, "id_field", "") or ""
    if not id_field or not rows:
        return rows, 0, []

    buckets: dict[str, list[int]] = {}
    order: list = []          # ("k", key) | ("r", row) — без id хвостом как есть
    for i, r in enumerate(rows):
        k = row_id(r, id_field)
        if k is None:
            order.append(("r", r))
            continue
        if k not in buckets:
            order.append(("k", k))
        buckets.setdefault(k, []).append(i)

    if all(len(v) == 1 for v in buckets.values()):
        return rows, 0, []

    removed, dup_examples = 0, []
    kept = []
    for kind, val in order:
        if kind == "r":
            kept.append(val)
            continue
        idxs = buckets[val]
        keep_idx = _best_idx(rows, idxs)
        kept.append(rows[keep_idx])
        if len(idxs) > 1:
            removed += len(idxs) - 1
            if len(dup_examples) < 5:
                dup_examples.append(val)
    return kept, removed, dup_examples
