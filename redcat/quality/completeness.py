"""
ЗАПОЛНЕННОСТЬ ДАННЫХ (completeness)
==================================
Отвечает на три вопроса:

  1. По полям: какие колонки таблицы пустые и насколько (доля заполненных
     значений в процентах).
  2. По записям: у каких записей не хватает данных — процент заполненности
     каждой записи и явный список отсутствующих полей («чего не хватает»).
  3. По группам: есть ли ЖК/застройщики, у которых поля систематически пустые
     (не одна битая запись, а проблема всей группы).

Общий процент по таблице — взвешенный: обязательные поля (required_fields из
описания источника) весят больше опциональных, поэтому пустая цена «бьёт»
сильнее, чем пустой тип отделки.

Модуль ничего не пишет: работает через dataops (read-only SQLite).

Запуск вручную (без веб-приложения):
    python -m redcat.quality.completeness
    python -m redcat.quality.completeness --table apartments
    python -m redcat.quality.completeness --table apartments --group housing_complex_name
    python -m redcat.quality.completeness --threshold 50 --top 30
"""

from __future__ import annotations

from redcat.core import paths
from collections import Counter, defaultdict
from pathlib import Path

from redcat.data import dataops

# ──────────────────────────────────────────────────────────────
#  Пути по умолчанию
# ──────────────────────────────────────────────────────────────
# Те же, что в webapp.py / redcat_scraper.py / demo_data.py: база собранных
# данных лежит в reports/redcat_data.db рядом с приложением. Константы
# объявлены здесь, чтобы модуль можно было запускать автономно (см. блок
# __main__) и чтобы не дублировать путь в каждом вызове.
BASE_DIR = paths.ROOT
OUTPUT_DIR = BASE_DIR / "reports"
DATA_DB = OUTPUT_DIR / "redcat_data.db"

# Веса типов полей при подсчёте общего процента заполненности
WEIGHTS = {"required": 1.0, "numeric": 0.7, "other": 0.3}

MIN_GROUP_SIZE = 3        # меньше записей в группе — не показываем

# Текстовые заглушки, которые формально не пусты, но означают «данных нет».
# API нередко отдаёт вместо числа или даты строку вроде «Цена по запросу»
# или «-». Без этого списка такие ячейки считались бы заполненными, и
# заполненность была бы завышена.
_EMPTY_MARKERS = frozenset({
    "", "-", "—", "–", "n/a", "na", "none", "null", "нет", "нет данных",
    "не указано", "не указан", "не указана", "не задано", "по запросу",
    "цена по запросу", "уточняйте", "уточнить", "unknown", "undefined",
    "nan", "?", "??",
})


def _is_empty(v) -> bool:
    """Пусто ли значение по смыслу (учитывая текстовые заглушки)."""
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip().lower()
        return s in _EMPTY_MARKERS
    if isinstance(v, (list, dict)):
        return not v
    return False


def _kind(field: str, spec) -> str:
    if spec:
        if field in (spec.required_fields or []):
            return "required"
        if field in (spec.numeric_fields or []):
            return "numeric"
    return "other"


# ──────────────────────────────────────────────────────────────
#  1. По полям
# ──────────────────────────────────────────────────────────────
def completeness_by_field(db_path=DATA_DB, table=None,
                          filters=None, match="AND") -> list:
    """Заполненность каждой колонки таблицы (обёртка над column_stats).

    db_path можно не передавать — по умолчанию reports/redcat_data.db
    рядом с приложением (константа DATA_DB в начале модуля).
    """
    cols = dataops.columns(db_path, table)
    out = []
    for c in cols:
        s = dataops.column_stats(db_path, table, c["name"], filters, match)
        s["kind"] = _kind(c["name"],
                          None)  # kind подставит вызывающий, где есть spec
        out.append(s)
    return out


def overall_score(by_field, spec=None, min_fill=5.0) -> dict:
    """Один взвешенный процент заполненности по таблице.

    Колонки, заполненные меньше min_fill процентов, исключаются из расчёта:
    это фантомные поля от вложенных объектов — они есть в схеме, но API их
    не отдаёт вовсе. Считать их значимыми — значит занижать общий балл за
    то, чего в источнике нет.
    """
    if not by_field:
        return {"pct": None, "fields": 0, "excluded": 0}
    relevant = [f for f in by_field if f["fill_rate"] >= min_fill]
    excluded = len(by_field) - len(relevant)
    if not relevant:
        return {"pct": None, "fields": 0, "excluded": excluded}
    weights, filled = [], []
    for f in relevant:
        w = WEIGHTS[_kind(f["field"], spec)]
        weights.append(w)
        filled.append(w * f["fill_rate"] / 100)
    total_w = sum(weights)
    return {
        "pct": round(100 * sum(filled) / total_w, 1) if total_w else None,
        "fields": len(relevant),
        "excluded": excluded,
    }


# ──────────────────────────────────────────────────────────────
#  2. По записям
# ──────────────────────────────────────────────────────────────
def _record_fields(spec, cols) -> list:
    """Поля, по которым оцениваем запись.

    Если у источника заданы required_fields — берём их плюс числовые поля.
    Иначе — все колонки таблицы (кроме явно служебных): для таблиц без
    описания заполненность всё равно посчитается, просто без приоритета.
    """
    names = [c["name"] for c in cols if c["name"] != "run_id"]
    if spec and spec.required_fields:
        fields = [f for f in spec.required_fields if f in names]
        for f in (spec.numeric_fields or []):
            if f in names and f not in fields:
                fields.append(f)
        return fields
    return names


def completeness_by_record(db_path=DATA_DB, table=None, spec=None,
                           filters=None, match="AND", limit=200000,
                           min_fill=5.0, precomputed_fields=None) -> list:
    """Каждая запись: id, название, % заполненности, список пустых полей.

    Проверяются «значащие» колонки — те, что реально заполнены хотя бы у
    min_fill% записей. Это выравнивает расчёт с тем, что показывает блок
    «Поля: доля заполненных значений»: если колонка где-то не пуста, её
    пропуск у конкретной записи тоже считается.

    Колонки, заполненные на 0% (фантомные поля от вложенных объектов —
    `image`, `land_area`, `flat_plan_image`), исключаются: они не различают
    записи между собой, но занижали бы процент у всех сразу.

    Обязательные поля из spec.required_fields включаются всегда, даже если
    их fill_rate низкий: это сигнал о проблеме источника.

    precomputed_fields — результат completeness_by_field() для той же
    таблицы/фильтров, если вызывающий уже его посчитал. Без этого параметра
    fill_rate каждой колонки считается заново отдельным SQL-агрегатом; на
    вкладке «Заполненность» это тот же самый расчёт, что уже сделан для
    блока «Поля», и его незачем повторять.
    """
    cols = dataops.columns(db_path, table)
    names = [c["name"] for c in cols if c["name"] != "run_id"]
    if not names:
        return []
    id_field = (spec.id_field if spec and spec.id_field in names
                else ("id" if "id" in names else names[0]))
    name_field = (spec.name_field if spec and spec.name_field in names
                  else next((n for n in names if "name" in n.lower()), id_field))

    # Отбираем «значащие» колонки: смотрим fill_rate по каждой.
    # Если fill_rate уже посчитан вызывающим (precomputed_fields) —
    # переиспользуем его вместо ещё одного SQL-агрегата на каждую колонку.
    if precomputed_fields is not None:
        rates = {f["field"]: f["fill_rate"] for f in precomputed_fields}
        fields = [f for f in names if rates.get(f, 0) >= min_fill]
    else:
        fields = []
        for f in names:
            try:
                s = dataops.column_stats(db_path, table, f, filters, match)
            except dataops.DataError:
                continue
            if s["fill_rate"] >= min_fill:
                fields.append(f)

    # Обязательные поля из описания — всегда в списке, даже при низком
    # fill_rate: если они пусты, это как раз важно показать.
    if spec and spec.required_fields:
        for f in spec.required_fields:
            if f in names and f not in fields:
                fields.append(f)
    if not fields:
        return []

    select = list(dict.fromkeys([id_field, name_field] + fields))

    out = []
    stream = dataops.iter_all(db_path, table, filters, match,
                              select=select, limit=limit)
    next(stream, None)
    for row in stream:
        missing = [f for f in fields if f not in row or _is_empty(row.get(f))]
        total = len(fields)
        out.append({
            "id": row.get(id_field),
            "name": row.get(name_field),
            "pct": round(100 * (total - len(missing)) / total, 1),
            "missing": missing,
        })
    return out


# ──────────────────────────────────────────────────────────────
#  3. По группам (ЖК, застройщик и т.п.)
# ──────────────────────────────────────────────────────────────
def completeness_by_group(db_path=DATA_DB, table=None, spec=None,
                          group_field=None, filters=None,
                          match="AND", limit=100,
                          precomputed_fields=None) -> list:
    """Заполненность в разрезе группы.

    Для каждой группы: сколько записей, средний % заполненности, сколько
    записей хуже 80% и какие поля пустые чаще всего. Группы сортируются
    от худших — верх списка это то, что стоит проверить в первую очередь.

    precomputed_fields — см. completeness_by_record(): избегает повторного
    подсчёта fill_rate по колонкам, если он уже сделан вызывающим.
    """
    cols = dataops.columns(db_path, table)
    names = [c["name"] for c in cols]
    if group_field not in names:
        raise dataops.DataError(
            f"В таблице «{table}» нет колонки «{group_field}».")
    id_field = (spec.id_field if spec and spec.id_field in names
                else ("id" if "id" in names else names[0]))

    records = completeness_by_record(db_path, table, spec, filters, match,
                                     precomputed_fields=precomputed_fields)

    # id → значение группы (второй проход, только два поля)
    gmap = {}
    stream = dataops.iter_all(db_path, table, filters, match,
                              select=[id_field, group_field], limit=200000)
    next(stream, None)
    for row in stream:
        gmap[str(row.get(id_field))] = row.get(group_field)

    groups = defaultdict(list)
    for r in records:
        groups[str(gmap.get(str(r["id"])))].append(r)

    out = []
    for gval, members in groups.items():
        if len(members) < MIN_GROUP_SIZE:
            continue
        miss_counter = Counter()
        for m in members:
            miss_counter.update(m["missing"])
        out.append({
            "group": gval,
            "records": len(members),
            "avg_pct": round(sum(m["pct"] for m in members) / len(members), 1),
            "below_80": sum(1 for m in members if m["pct"] < 80),
            "worst_fields": [f for f, _ in miss_counter.most_common(3)],
        })
    out.sort(key=lambda x: (x["avg_pct"], -x["records"]))
    return out[:limit]


# ──────────────────────────────────────────────────────────────
#  Запуск вручную: python -m redcat.quality.completeness [--table ...] [--group ...]
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import sys

    # Чтобы `import sources` работал и при запуске из другой папки,
    # добавляем директорию модуля в начало sys.path.
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))

    try:
        from redcat.sources import registry as src
    except ImportError:
        src = None

    ap = argparse.ArgumentParser(
        description="Заполненность данных: по полям, записям и группам.")
    ap.add_argument("--db", default=str(DATA_DB),
                    help=f"база данных (по умолчанию {DATA_DB})")
    ap.add_argument("--table", help="таблица (по умолчанию все)")
    ap.add_argument("--group", help="поле-разрез для групповой сводки, "
                                    "например housing_complex_name")
    ap.add_argument("--threshold", type=float, default=80.0,
                    help="порог проблемной записи в процентах (по умолчанию 80)")
    ap.add_argument("--top", type=int, default=20,
                    help="сколько худших записей и групп показать")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"❌ База не найдена: {db_path}")
        print("   Соберите данные или создайте демо-набор: python -m redcat.tools.demo_data")
        sys.exit(1)

    # Описания источников нужны, чтобы веса полей и «обязательность» были
    # правильными — без spec _kind() вернёт "other" для всего.
    specs = {}
    if src is not None:
        try:
            src.load_from_dir(paths.SOURCES_DIR)
            specs = {s.key: s for s in src.all_sources()}
        except Exception as e:
            print(f"⚠️ Источники не загружены ({type(e).__name__}: {e}). "
                  f"Веса полей будут по умолчанию.")

    tables = ([args.table] if args.table
              else [t["name"] for t in dataops.list_tables(db_path)])

    for table in tables:
        if not table or table.startswith("_"):
            continue
        spec = specs.get(table)
        title = f" — {spec.title}" if spec and spec.title else ""
        print(f"\n{'═' * 62}")
        print(f"  Таблица: {table}{title}")
        print("═" * 62)

        try:
            fields = completeness_by_field(db_path, table)
        except dataops.DataError as e:
            print(f"  ⚠️ {e}")
            continue

        for f in fields:
            f["kind"] = _kind(f["field"], spec)
        overall = overall_score(fields, spec)
        print(f"  Взвешенная заполненность: {overall['pct']}% "
              f"по {overall['fields']} полям")

        print("\n  Хуже всего заполнены:")
        for f in sorted(fields, key=lambda x: x["fill_rate"])[:8]:
            mark = " *" if f["kind"] == "required" else ""
            print(f"    {f['fill_rate']:6.1f}%  {f['field']}{mark}")

        recs = completeness_by_record(db_path, table, spec)
        bad = sorted((r for r in recs if r["pct"] < args.threshold),
                     key=lambda r: r["pct"])
        print(f"\n  Записей ниже {args.threshold:g}%: "
              f"{len(bad)} из {len(recs)}")
        for r in bad[:args.top]:
            miss = ", ".join(r["missing"][:4]) or "—"
            print(f"    {r['pct']:5.1f}%  {r['name'] or r['id']}: {miss}")

        if args.group:
            print(f"\n  В разрезе «{args.group}»:")
            try:
                groups = completeness_by_group(
                    db_path, table, spec, args.group)
            except dataops.DataError as e:
                print(f"    ⚠️ {e}")
                continue
            for g in groups[:args.top]:
                print(f"    {g['avg_pct']:5.1f}%  {g['group']}  "
                      f"({g['records']} записей, хуже 80%: {g['below_80']})")