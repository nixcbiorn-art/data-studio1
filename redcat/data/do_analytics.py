"""do_analytics — группировки, корреляции, временные ряды, Парето."""
from __future__ import annotations

import statistics
from redcat.data.do_core import AGGREGATIONS, AGGREGATION_LABELS, DataError, _check_columns, _col_sql, _safe_ident, connect_ro
from redcat.data.do_query import build_where
from redcat.data.do_stats import pivot


def group_summary(db_path, table, dimension, metric, filters=None, match="AND",
                  limit=60) -> dict:
    """Полная сводка по каждой группе одним запросом.

    Отличие от обычной сводной: там одна агрегация за раз, и чтобы понять
    группу, приходится строить её несколько раз. Здесь сразу количество,
    среднее, медиана, квартили и края — то есть видно не только «сколько», но
    и насколько группа однородна. Большой разрыв между медианой и средним —
    признак, что в группе есть перекос, и по среднему судить нельзя.
    """
    with connect_ro(db_path) as conn:
        _check_columns(conn, table, [dimension, metric])
        _, num = _col_sql(conn, table, metric)
        dim = f'"{_safe_ident(dimension)}"'
        where, args = build_where(conn, table, filters or [], match)
        rows = conn.execute(
            f'SELECT {dim} AS _g, COUNT(*) AS rows_count, '
            f'COUNT({num}) AS filled, AVG({num}) AS avg, MEDIAN({num}) AS median, '
            f'MIN({num}) AS min, MAX({num}) AS max, PCT25({num}) AS p25, '
            f'PCT75({num}) AS p75, SUM({num}) AS sum '
            f'FROM "{_safe_ident(table)}" {where} GROUP BY {dim} '
            f'ORDER BY rows_count DESC LIMIT ?', args + [limit]).fetchall()

    out = []
    for r in rows:
        item = {"group": r["_g"], "rows_count": r["rows_count"], "filled": r["filled"]}
        for key in ("avg", "median", "min", "max", "p25", "p75", "sum"):
            value = r[key]
            item[key] = round(value, 2) if isinstance(value, float) else value
        # разброс внутри группы: насколько середина отличается от краёв
        if item["p75"] is not None and item["p25"] is not None:
            item["iqr"] = round(item["p75"] - item["p25"], 2)
        out.append(item)
    return {"dimension": dimension, "metric": metric, "groups": out}


def correlation(db_path, table, x_field, y_field, filters=None, match="AND",
                sample=20000, points=400) -> dict:
    """Связь двух числовых полей: коэффициент Пирсона + точки для графика.

    Отвечает на вопрос «растёт ли одно вместе с другим»: например, растёт ли
    цена за м² с этажом. Важная оговорка, которую возвращаем вместе с числом:
    связь — это не причина. Совпадение динамики может объясняться третьим
    фактором, которого нет в этих двух колонках.
    """
    with connect_ro(db_path) as conn:
        _check_columns(conn, table, [x_field, y_field])
        _, xnum = _col_sql(conn, table, x_field)
        _, ynum = _col_sql(conn, table, y_field)
        where, args = build_where(conn, table, filters or [], match)
        rows = conn.execute(
            f'SELECT {xnum} AS x, {ynum} AS y FROM "{_safe_ident(table)}" {where} '
            f'LIMIT ?', args + [sample]).fetchall()

    pairs = [(float(r["x"]), float(r["y"])) for r in rows
             if r["x"] is not None and r["y"] is not None]
    if len(pairs) < 3:
        return {"x_field": x_field, "y_field": y_field, "n": len(pairs), "r": None,
                "message": "Слишком мало строк, где заполнены оба поля."}

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((a - mx) * (b - my) for a, b in pairs)
    den_x = sum((a - mx) ** 2 for a in xs)
    den_y = sum((b - my) ** 2 for b in ys)
    r = num / ((den_x * den_y) ** 0.5) if den_x and den_y else None

    # линия тренда (метод наименьших квадратов) — чтобы связь была видна глазом
    slope = num / den_x if den_x else None
    intercept = my - slope * mx if slope is not None else None

    step = max(1, len(pairs) // points)
    return {
        "x_field": x_field, "y_field": y_field, "n": len(pairs),
        "r": round(r, 4) if r is not None else None,
        "strength": _describe_correlation(r),
        "slope": slope, "intercept": intercept,
        "points": [{"x": a, "y": b} for a, b in pairs[::step]][:points],
    }


def _describe_correlation(r) -> str:
    if r is None:
        return "не определена"
    a = abs(r)
    direction = "прямая" if r > 0 else "обратная"
    if a < 0.2:
        return "связи практически нет"
    if a < 0.4:
        return f"слабая {direction} связь"
    if a < 0.6:
        return f"умеренная {direction} связь"
    if a < 0.8:
        return f"заметная {direction} связь"
    return f"сильная {direction} связь"


GRANULARITY = {
    "day": ("substr(CAST({col} AS TEXT), 1, 10)", "по дням"),
    "month": ("substr(CAST({col} AS TEXT), 1, 7)", "по месяцам"),
    "year": ("substr(CAST({col} AS TEXT), 1, 4)", "по годам"),
    "week": ("strftime('%Y-W%W', date(substr(CAST({col} AS TEXT), 1, 10)))", "по неделям"),
}


def timeseries(db_path, table, date_field, granularity="month", metric=None,
               agg="count", filters=None, match="AND", limit=400) -> dict:
    """Динамика внутри самих данных по колонке с датой.

    Это другое измерение времени, чем история запусков: там видно, как менялся
    снимок рынка от сбора к сбору, а здесь — как распределены сами записи по
    их собственной дате (когда лот появился, когда начал действовать регламент).
    """
    if granularity not in GRANULARITY:
        raise DataError(f"Неизвестная детализация: {granularity}")
    if agg not in AGGREGATIONS:
        raise DataError(f"Неизвестная агрегация: {agg}")

    with connect_ro(db_path) as conn:
        _check_columns(conn, table, [date_field])
        bucket = GRANULARITY[granularity][0].format(
            col=f'"{_safe_ident(date_field)}"')
        if agg == "count" and not metric:
            agg_sql = "COUNT(*)"
        else:
            if not metric:
                raise DataError(f"Для агрегации «{AGGREGATION_LABELS[agg]}» "
                                f"нужно выбрать поле.")
            _check_columns(conn, table, [metric])
            col, num = _col_sql(conn, table, metric)
            agg_sql = AGGREGATIONS[agg].format(col=col, num=num)

        where, args = build_where(conn, table, filters or [], match)
        tail = "AND" if where else "WHERE"
        rows = conn.execute(
            f'SELECT {bucket} AS period, {agg_sql} AS value, COUNT(*) AS rows_count '
            f'FROM "{_safe_ident(table)}" {where} {tail} {bucket} IS NOT NULL '
            f"AND {bucket} <> '' "
            f'GROUP BY period ORDER BY period LIMIT ?', args + [limit]).fetchall()

    data = [{"period": r["period"],
             "value": round(r["value"], 2) if isinstance(r["value"], float) else r["value"],
             "rows_count": r["rows_count"]} for r in rows]
    return {"date_field": date_field, "granularity": granularity,
            "granularity_label": GRANULARITY[granularity][1],
            "agg": agg, "agg_label": AGGREGATION_LABELS[agg],
            "metric": metric, "data": data}


def pareto(db_path, table, dimension, metric=None, agg="count",
           filters=None, match="AND", limit=200) -> dict:
    """ABC-анализ: какая доля групп даёт основную часть объёма.

    Практический смысл — понять, на чём сосредоточиться. Если 80% предложения
    приходится на 6 застройщиков из 40, то следить в первую очередь нужно за
    ними, а остальные можно проверять реже.
    """
    result = pivot(db_path, table, [dimension], metric, agg, filters, match,
                   limit=limit, sort_desc=True)
    rows = [r for r in result["data"]
            if isinstance(r["value"], (int, float)) and r["value"] > 0]
    total = sum(r["value"] for r in rows)
    if not total:
        return {"dimension": dimension, "total": 0, "data": [],
                "message": "Нет положительных значений для разбора."}

    cumulative = 0.0
    data = []
    a_count = b_count = 0
    for r in rows:
        cumulative += r["value"]
        share = 100 * r["value"] / total
        cum_share = 100 * cumulative / total
        if cum_share <= 80:
            group = "A"
            a_count += 1
        elif cum_share <= 95:
            group = "B"
            b_count += 1
        else:
            group = "C"
        data.append({dimension: r[dimension], "value": r["value"],
                     "share": round(share, 2), "cumulative": round(cum_share, 2),
                     "abc": group})
    return {"dimension": dimension, "metric": metric, "agg_label": result["agg_label"],
            "total": round(total, 2), "data": data,
            "a_count": a_count, "b_count": b_count,
            "c_count": len(data) - a_count - b_count,
            "summary": f"{a_count} из {len(data)} групп дают 80% объёма"}
