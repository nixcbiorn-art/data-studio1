"""
Поиск аномалий
==============
Два уровня, оба полностью универсальные — ничего не знают про недвижимость
и работают с любым источником:

1. По истории запусков (временной ряд метрик): резкие скачки объёма, обвалы
   полноты сбора, сдвиг уровня цен, «залипшая» метрика, аномальная
   длительность сбора.
2. По самим данным запуска: выбросы в числовых полях (в том числе внутри
   групп — по ЖК, по застройщику), невозможные значения, дубликаты,
   всплеск пропусков, исчезнувшие группы, и — если есть история записей —
   аномальные изменения значений у конкретной записи.

Методы устойчивы к выбросам: везде, где можно, используется медиана и MAD,
а не среднее и σ, потому что одно экстремальное значение ломает среднее и
маскирует остальные аномалии.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import statistics

# Порог модифицированного z-score. 3.5 — общепринятая отсечка (Iglewicz & Hoaglin).
DEFAULT_Z = 3.5
MIN_HISTORY = 4       # меньше точек — статистика не имеет смысла
MIN_GROUP_SIZE = 8    # меньше записей в группе — выбросы не ищем

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if isinstance(v, float) and (math.isnan(v) or math.isinf(v)) else float(v)
    try:
        return float(str(v).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        if abs(v) >= 1_000_000:
            return f"{v/1_000_000:.2f} млн"
        if abs(v) >= 1000:
            return f"{v:,.0f}".replace(",", "\u202f")
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def robust_stats(values):
    """Медиана и MAD набора — считаются ОДИН раз на набор, а не на каждое значение.

    Раньше modified_zscore заново считал медиану и MAD по всему списку для
    каждого его элемента: O(n²·log n). На 10 000 записей это терпимо, на 24 000
    — минуты, а на полном сборе (~59 000) шаг «Поиск аномалий» не заканчивался.
    None, если значений меньше трёх.
    """
    if len(values) < 3:
        return None
    med = statistics.median(values)
    devs = [abs(v - med) for v in values]
    mad = statistics.median(devs)
    # MAD нулевой — ряд почти константа; переходим на среднее отклонение
    mean_dev = statistics.fmean(devs) if mad == 0 else None
    return med, mad, mean_dev


def zscore_from_stats(stats, value):
    """Модифицированный z-score по готовым (медиана, MAD, среднее_отклонение)."""
    if stats is None:
        return None
    med, mad, mean_dev = stats
    if mad == 0:
        if not mean_dev:
            return None
        return 0.7979 * (value - med) / mean_dev
    return 0.6745 * (value - med) / mad


def modified_zscore(values, value):
    """Модифицированный z-score на медиане и MAD. None, если разброса нет."""
    return zscore_from_stats(robust_stats(values), value)


def _anomaly(source, kind, severity, message, **extra):
    a = {
        "source": source, "kind": kind, "severity": severity, "message": message,
        "entity": extra.get("entity", ""), "metric": extra.get("metric", ""),
        "value": extra.get("value"), "expected": extra.get("expected"),
        "deviation": extra.get("deviation"),
    }
    return a


# ──────────────────────────────────────────────────────────────
#  1. АНОМАЛИИ ПО ИСТОРИИ ЗАПУСКОВ
# ──────────────────────────────────────────────────────────────
def detect_history_anomalies(history, metrics=None, z_threshold=DEFAULT_Z,
                             source="run_history"):
    """Ищет аномалии в последнем запуске относительно всей предыдущей истории.

    history: список словарей-запусков (хронологически), последний — текущий.
    metrics: какие поля проверять. По умолчанию — все числовые.
    """
    anomalies = []
    if len(history) < 2:
        return anomalies

    # Запуски с неполной загрузкой исключаем из базы сравнения: их метрики
    # занижены не из-за рынка, а из-за сети — иначе они задают ложную норму.
    baseline_runs = [r for r in history[:-1] if not r.get("incomplete")]
    current = history[-1]

    if metrics is None:
        metrics = [
            k for k, v in current.items()
            if k not in ("run_id", "incomplete") and _num(v) is not None
            and not k.endswith("_at")
        ]

    for metric in metrics:
        cur = _num(current.get(metric))
        if cur is None:
            continue
        series = [_num(r.get(metric)) for r in baseline_runs]
        series = [v for v in series if v is not None]
        if len(series) < MIN_HISTORY - 1:
            continue

        z = modified_zscore(series, cur)
        med = statistics.median(series)
        if z is not None and abs(z) > z_threshold:
            direction = "выше" if z > 0 else "ниже"
            sev = "critical" if abs(z) > z_threshold * 2 else "warning"
            anomalies.append(_anomaly(
                source, "metric_outlier", sev,
                f"«{metric}»: {_fmt(cur)} — резко {direction} обычного "
                f"(медиана по истории {_fmt(med)}, отклонение z={z:.1f}).",
                metric=metric, value=cur, expected=med, deviation=round(z, 2),
            ))

        # Отдельно — резкий скачок относительно непосредственно прошлого запуска
        prev = _num(baseline_runs[-1].get(metric)) if baseline_runs else None
        if prev and prev != 0:
            change = (cur - prev) / abs(prev) * 100
            if abs(change) >= 35 and (z is None or abs(z) <= z_threshold):
                anomalies.append(_anomaly(
                    source, "sudden_change", "warning",
                    f"«{metric}»: изменение на {change:+.1f}% за один запуск "
                    f"({_fmt(prev)} → {_fmt(cur)}).",
                    metric=metric, value=cur, expected=prev, deviation=round(change, 1),
                ))

    # Полнота сбора — отдельная проверка, это вопрос доверия к данным
    coverage = _num(current.get("coverage_pct"))
    if coverage is not None and coverage < 95:
        anomalies.append(_anomaly(
            source, "coverage_drop", "critical" if coverage < 80 else "warning",
            f"Собрано лишь {coverage:.1f}% от заявленного API объёма — "
            f"выводы по этому запуску делать нельзя.",
            metric="coverage_pct", value=coverage, expected=100,
        ))

    if current.get("incomplete"):
        anomalies.append(_anomaly(
            source, "incomplete_run", "critical",
            "Запуск помечен как неполный: часть страниц не догрузилась. "
            "Метрики занижены, в сравнение динамики не годятся.",
            metric="incomplete", value=1, expected=0,
        ))

    # «Залипшая» метрика: раньше менялась, а теперь N запусков подряд одинакова.
    for metric in metrics:
        tail = [_num(r.get(metric)) for r in history[-4:]]
        tail = [v for v in tail if v is not None]
        earlier = [_num(r.get(metric)) for r in history[:-4]]
        earlier = [v for v in earlier if v is not None]
        if len(tail) == 4 and len(set(tail)) == 1 and len(set(earlier)) > 1:
            anomalies.append(_anomaly(
                source, "frozen_metric", "warning",
                f"«{metric}» не меняется 4 запуска подряд ({_fmt(tail[0])}), "
                f"хотя раньше менялась — возможно, источник отдаёт кэш.",
                metric=metric, value=tail[0],
            ))

    return anomalies


# ──────────────────────────────────────────────────────────────
#  2. АНОМАЛИИ ВНУТРИ ДАННЫХ ЗАПУСКА
# ──────────────────────────────────────────────────────────────
def detect_data_anomalies(rows, spec, z_threshold=DEFAULT_Z, max_per_kind=50):
    """Ищет аномалии в собранных записях по описанию источника (SourceSpec)."""
    anomalies = []
    if not rows:
        return anomalies

    source = spec.key
    id_field = spec.id_field
    name_field = spec.name_field

    # --- дубликаты идентификаторов ---
    if id_field and id_field in rows[0]:
        seen, dupes = set(), set()
        for r in rows:
            rid = r.get(id_field)
            if rid is None:
                continue
            if rid in seen:
                dupes.add(rid)
            seen.add(rid)
        if dupes:
            anomalies.append(_anomaly(
                source, "duplicate_ids", "warning",
                f"Найдено {len(dupes)} повторяющихся идентификаторов "
                f"(например: {', '.join(map(str, list(dupes)[:3]))}). "
                f"Записи могут затирать друг друга при сравнении.",
                metric=id_field, value=len(dupes), expected=0,
            ))

    # --- обязательные поля и невозможные значения ---
    total = len(rows)
    for fld in spec.required_fields:
        missing = sum(1 for r in rows if r.get(fld) in (None, "", []))
        if missing:
            pct = 100 * missing / total
            anomalies.append(_anomaly(
                source, "missing_required", "critical" if pct > 20 else "warning",
                f"Поле «{fld}» пустое у {missing} записей ({pct:.1f}%).",
                metric=fld, value=missing, expected=0, deviation=round(pct, 1),
            ))

    for fld in spec.positive_fields:
        bad = [r for r in rows if (_num(r.get(fld)) is not None and _num(r.get(fld)) <= 0)]
        if bad:
            anomalies.append(_anomaly(
                source, "impossible_value", "critical",
                f"Поле «{fld}» имеет нулевое или отрицательное значение "
                f"у {len(bad)} записей — так быть не должно.",
                metric=fld, value=len(bad), expected=0,
                entity=str(bad[0].get(id_field, "")),
            ))

    # --- выбросы в числовых полях: глобально и внутри групп ---
    for fld in spec.numeric_fields:
        values = [(r, _num(r.get(fld))) for r in rows]
        values = [(r, v) for r, v in values if v is not None]
        if len(values) < MIN_GROUP_SIZE:
            continue

        nums = [v for _, v in values]
        nstats = robust_stats(nums)
        found = 0
        for r, v in values:
            z = zscore_from_stats(nstats, v)
            if z is not None and abs(z) > z_threshold * 2:  # глобально — строже
                found += 1
                if found <= max_per_kind:
                    anomalies.append(_anomaly(
                        source, "value_outlier", "info",
                        f"«{fld}» = {_fmt(v)} у записи "
                        f"«{r.get(name_field) or r.get(id_field)}» — "
                        f"сильно выбивается из общего распределения (z={z:.1f}).",
                        entity=str(r.get(id_field, "")), metric=fld,
                        value=v, expected=nstats[0], deviation=round(z, 2),
                    ))
        if found > max_per_kind:
            anomalies.append(_anomaly(
                source, "value_outlier", "info",
                f"…и ещё {found - max_per_kind} выбросов по полю «{fld}» "
                f"(показаны первые {max_per_kind}).",
                metric=fld, value=found,
            ))

        # внутри групп — так ловятся «дешёвая квартира в дорогом ЖК»
        for gfld in spec.group_fields:
            groups = {}
            for r, v in values:
                groups.setdefault(r.get(gfld), []).append((r, v))
            shown = 0
            for gval, members in groups.items():
                if gval is None or len(members) < MIN_GROUP_SIZE:
                    continue
                gnums = [v for _, v in members]
                gstats = robust_stats(gnums)
                for r, v in members:
                    z = zscore_from_stats(gstats, v)
                    if z is not None and abs(z) > z_threshold and shown < max_per_kind:
                        shown += 1
                        anomalies.append(_anomaly(
                            source, "group_outlier", "warning",
                            f"«{fld}» = {_fmt(v)} у «{r.get(name_field) or r.get(id_field)}» "
                            f"выбивается внутри группы {gfld}=«{gval}» "
                            f"(медиана по группе {_fmt(gstats[0])}, z={z:.1f}).",
                            entity=str(r.get(id_field, "")), metric=fld, value=v,
                            expected=gstats[0], deviation=round(z, 2),
                        ))

    return anomalies


# ──────────────────────────────────────────────────────────────
#  2b. ОДИНАКОВЫЕ ЗНАЧЕНИЯ У РАЗНЫХ ЗАПИСЕЙ («одинаковые цены»)
# ──────────────────────────────────────────────────────────────
def detect_identical_value_clusters(rows, spec, min_unique_ratio=0.3, min_count=8,
                                    share_threshold=0.05, group_share_threshold=0.3,
                                    max_per_kind=20):
    """Ищет числовые поля, где одно и то же значение делят слишком много
    РАЗНЫХ записей — например, десятки лотов с абсолютно одинаковой ценой.

    Само по себе совпадение — не аномалия (два лота вполне могут стоить
    одинаково). Аномалия — когда поле по своей природе почти непрерывное
    (у большинства записей разное значение, как у цены), а какое-то одно
    значение вдруг покрывает заметную долю записей: обычно это значит, что
    часть данных не обновилась, пришла с дефолтным/плейсхолдерным значением,
    или несколько записей на самом деле задублированы.

    Поля, где повторы — норма (этаж, число комнат: у них и так мало разных
    значений на большую выборку), отсеиваются через min_unique_ratio —
    сначала проверяем, что в этом источнике поле обычно ПОЧТИ уникально.
    """
    anomalies = []
    if not rows or not spec.numeric_fields:
        return anomalies
    source = spec.key
    id_field, name_field = spec.id_field, spec.name_field

    for fld in spec.numeric_fields:
        values = [(r, _num(r.get(fld))) for r in rows]
        values = [(r, v) for r, v in values if v is not None and v != 0]
        n = len(values)
        if n < min_count * 2:
            continue
        counts = {}
        for r, v in values:
            counts.setdefault(v, []).append(r)
        if len(counts) / n < min_unique_ratio:
            continue  # поле по природе дискретное — повторы тут ожидаемы

        # --- глобально: одно значение покрывает заметную долю ВСЕХ записей ---
        shown = 0
        for v, members in sorted(counts.items(), key=lambda kv: -len(kv[1])):
            share = len(members) / n
            if len(members) < min_count or share < share_threshold:
                break  # список отсортирован по убыванию — дальше будет только меньше
            shown += 1
            if shown > max_per_kind:
                break
            sample = ", ".join(str(m.get(id_field, "")) for m in members[:3])
            anomalies.append(_anomaly(
                source, "identical_value_cluster",
                "critical" if share >= 0.15 else "warning",
                f"«{fld}» = {_fmt(v)} одинаково у {len(members)} разных записей "
                f"({share*100:.1f}% от всех, где поле заполнено), например: {sample}. "
                f"Для почти непрерывного поля так быть не должно — похоже на "
                f"незаполненные/дефолтные значения или задвоенные записи.",
                metric=fld, value=len(members),
                expected=round(n / max(len(counts), 1), 2),
                deviation=round(share * 100, 1),
            ))

        # --- внутри групп: N лотов ОДНОГО ЖК/застройщика по одной цене ---
        for gfld in spec.group_fields:
            groups = {}
            for r, v in values:
                groups.setdefault(r.get(gfld), []).append((r, v))
            gshown = 0
            for gval, members in groups.items():
                if gval is None or len(members) < MIN_GROUP_SIZE:
                    continue
                gcounts = {}
                for r, v in members:
                    gcounts.setdefault(v, []).append(r)
                v, recs = max(gcounts.items(), key=lambda kv: len(kv[1]))
                gshare = len(recs) / len(members)
                if len(recs) < min_count or gshare < group_share_threshold:
                    continue
                gshown += 1
                if gshown > max_per_kind:
                    continue
                anomalies.append(_anomaly(
                    source, "identical_value_cluster_group", "warning",
                    f"«{fld}» = {_fmt(v)} одинаково у {len(recs)} из {len(members)} "
                    f"записей внутри группы {gfld}=«{gval}» ({gshare*100:.0f}%) — "
                    f"похоже, часть записей не получила своё индивидуальное значение.",
                    entity=str(gval), metric=fld, value=len(recs), expected=len(members),
                    deviation=round(gshare * 100, 1),
                ))
    return anomalies


# ──────────────────────────────────────────────────────────────
#  2c. «ЗАСТЫВШИЕ» ЗНАЧЕНИЯ У КОНКРЕТНОЙ ЗАПИСИ (неизменность по истории)
# ──────────────────────────────────────────────────────────────
def detect_frozen_records(stats_db, source, rows, spec, min_runs=5,
                          max_frozen_share=0.5, max_items=30):
    """Ищет записи, чьё числовое поле не меняется много запусков подряд,
    хотя у большинства ДРУГИХ записей того же поля — меняется.

    Использует накопленную по-записям историю (record_history в run_stats.db):
    то, что раньше собиралось, но нигде не анализировалось. Один «застывший»
    лот на фоне живого рынка — рабочий сигнал (источник не обновляет цену,
    лот придержан и т.п.). Если «не меняется» почти всё поле сразу (например,
    этаж — он физически не может измениться) — это природа поля, а не
    аномалия, и такие поля отсеиваются через max_frozen_share.
    """
    anomalies = []
    if not rows or not spec.numeric_fields:
        return anomalies
    id_field, name_field = spec.id_field, spec.name_field
    current_ids = {str(r.get(id_field)) for r in rows if r.get(id_field) is not None}
    if len(current_ids) < MIN_GROUP_SIZE:
        return anomalies

    try:
        with sqlite3.connect(f"file:{stats_db}?mode=ro", uri=True, timeout=15) as conn:
            hist = conn.execute("""
                SELECT record_id, field, value FROM (
                    SELECT record_id, field, run_id, value,
                           ROW_NUMBER() OVER (
                               PARTITION BY record_id, field ORDER BY run_id DESC) AS rn
                    FROM record_history WHERE source = ?
                ) WHERE rn <= ?
            """, (source, min_runs)).fetchall()
    except sqlite3.Error as e:
        logging.warning("[%s] история записей недоступна: %s", source, e)
        return anomalies

    by_field = {}
    for rid, fld, value in hist:
        by_field.setdefault(fld, {}).setdefault(rid, []).append(value)

    names = {str(r.get(id_field)): r.get(name_field) for r in rows}
    for fld, per_record in by_field.items():
        eligible = {rid: vals for rid, vals in per_record.items()
                   if len(vals) >= min_runs and rid in current_ids}
        if len(eligible) < MIN_GROUP_SIZE:
            continue
        frozen = {rid: vals[0] for rid, vals in eligible.items() if len(set(vals)) == 1}
        frozen_share = len(frozen) / len(eligible)
        if frozen_share == 0 or frozen_share > max_frozen_share:
            # либо вообще ничего не застыло, либо застыло почти всё —
            # значит, поле такое по своей природе, это не аномалия.
            continue
        shown = 0
        for rid, value in frozen.items():
            if shown >= max_items:
                break
            shown += 1
            anomalies.append(_anomaly(
                source, "frozen_record_field", "info",
                f"«{fld}» у «{names.get(rid) or rid}» не меняется "
                f"{min_runs} запусков подряд ({_fmt(value)}), хотя у "
                f"{100 - frozen_share*100:.0f}% остальных записей это поле "
                f"за то же время менялось.",
                entity=rid, metric=fld, value=value,
                deviation=round(frozen_share * 100, 1),
            ))
        if len(frozen) > max_items:
            anomalies.append(_anomaly(
                source, "frozen_record_field", "info",
                f"Всего записей с «застывшим» полем «{fld}»: {len(frozen)} "
                f"(показаны первые {max_items}).",
                metric=fld, value=len(frozen),
            ))
    return anomalies


# ──────────────────────────────────────────────────────────────
#  3. АНОМАЛИИ ИЗМЕНЕНИЙ КОНКРЕТНЫХ ЗАПИСЕЙ
# ──────────────────────────────────────────────────────────────
def detect_record_change_anomalies(prev_snapshot, rows, spec, pct_threshold=25,
                                   max_items=50):
    """Сравнивает числовые поля записи с её же значением в прошлом запуске.

    Ловит то, что не видно в агрегатах: цена одного лота упала на 40%,
    площадь изменилась (такого быть не должно), лот «подорожал» вдвое.
    """
    anomalies = []
    if not prev_snapshot or not rows:
        return anomalies

    source = spec.key
    id_field, name_field = spec.id_field, spec.name_field
    shown = 0
    counts = {}

    for r in rows:
        rid = str(r.get(id_field))
        old = prev_snapshot.get(rid)
        if not old:
            continue
        for fld in spec.numeric_fields:
            new_v, old_v = _num(r.get(fld)), _num(old.get(fld))
            if new_v is None or old_v is None or old_v == 0:
                continue
            change = (new_v - old_v) / abs(old_v) * 100
            if abs(change) < pct_threshold:
                continue
            counts[fld] = counts.get(fld, 0) + 1
            if shown < max_items:
                shown += 1
                anomalies.append(_anomaly(
                    source, "record_jump", "warning",
                    f"«{r.get(name_field) or rid}»: поле «{fld}» изменилось на "
                    f"{change:+.1f}% ({_fmt(old_v)} → {_fmt(new_v)}).",
                    entity=rid, metric=fld, value=new_v, expected=old_v,
                    deviation=round(change, 1),
                ))

    for fld, cnt in counts.items():
        if cnt > max_items:
            anomalies.append(_anomaly(
                source, "record_jump", "info",
                f"Всего записей с резким изменением «{fld}»: {cnt} "
                f"(показаны первые {max_items}).",
                metric=fld, value=cnt,
            ))
    return anomalies


def detect_group_disappearance(prev_snapshot, rows, spec, min_share=0.5):
    """Ловит исчезновение целых групп (ЖК пропал из выдачи целиком)."""
    anomalies = []
    if not prev_snapshot or not spec.group_fields:
        return anomalies

    gfld = spec.group_fields[0]
    old_counts, new_counts = {}, {}
    for r in prev_snapshot.values():
        g = r.get(gfld)
        if g is not None:
            old_counts[g] = old_counts.get(g, 0) + 1
    for r in rows:
        g = r.get(gfld)
        if g is not None:
            new_counts[g] = new_counts.get(g, 0) + 1

    for g, old_n in old_counts.items():
        new_n = new_counts.get(g, 0)
        if old_n >= MIN_GROUP_SIZE and new_n <= old_n * min_share:
            pct = 100 * (old_n - new_n) / old_n
            anomalies.append(_anomaly(
                spec.key, "group_shrink", "critical" if new_n == 0 else "warning",
                f"{gfld}=«{g}»: записей стало {new_n} вместо {old_n} (−{pct:.0f}%)."
                + (" Группа исчезла полностью." if new_n == 0 else ""),
                entity=str(g), metric=gfld, value=new_n, expected=old_n,
                deviation=round(-pct, 1),
            ))
    return anomalies


def summarize(anomalies) -> dict:
    """Сводка по severity + сортировка от критичных к информационным."""
    ordered = sorted(anomalies, key=lambda a: (
        SEVERITY_ORDER.get(a["severity"], 9), a["source"], a["kind"]
    ))
    counts = {"critical": 0, "warning": 0, "info": 0}
    for a in anomalies:
        counts[a["severity"]] = counts.get(a["severity"], 0) + 1
    return {"items": ordered, "counts": counts, "total": len(anomalies)}
