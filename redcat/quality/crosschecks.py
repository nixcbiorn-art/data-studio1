"""
СКВОЗНЫЕ ПРОВЕРКИ
=================
Проверяют собранные данные как одно целое, а не каждую таблицу по
отдельности. Отвечают на вопрос «можно ли этим пользоваться».

Зачем отдельно от поиска аномалий: аномалии ищут необычное внутри одного
набора (выброс цены, странный скачок метрики). Здесь — другое: сходятся ли
таблицы между собой и не потерялась ли часть данных при сборе. Такие
поломки не выглядят «необычно», они выглядят нормально — просто цифры
меньше, чем должны быть.

Что проверяется:

  • Полнота: собрано против заявленного API общего числа.
  • Битые связи: ссылки на записи, которых нет в справочнике.
  • Покрытие: сколько записей справочника вообще представлено в дочерней
    таблице (много «пустых» ЖК — типичный след неполного сбора).
  • Дубликаты идентификаторов.
  • Пустые обязательные поля.
  • Невозможные значения (цена ≤ 0, площадь ≤ 0).
  • Рассогласование названий у одной и той же сущности в разных таблицах.

Модуль ничего не пишет: открывает базу только на чтение.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from redcat.data import dataops

CRITICAL, WARNING, INFO = "critical", "warning", "info"


def _issue(level, area, title, detail="", number=None, advice=""):
    return {"level": level, "area": area, "title": title,
            "detail": detail, "number": number, "advice": advice}


def _singularize(name: str) -> str:
    """housing_complexes -> housing_complex. Грубо, но для имён таблиц хватает.

    Порядок правил важен: «complexes» нельзя резать простым отбрасыванием «s»,
    иначе получится «complexe», и связь с колонкой `housing_complex_id`
    не найдётся.
    """
    if name.endswith("ies") and len(name) > 3:
        return name[:-3] + "y"
    if name.endswith("es") and re.search(r"(x|s|z|ch|sh)es$", name):
        return name[:-2]
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


def detect_relations(db_path, specs=None) -> list:
    """Находит связи «дочерняя таблица → справочник» без ручной настройки.

    Сначала берём то, что явно указано в описаниях источников
    (split_values_from — это и есть связь). Затем добавляем связи, угаданные
    по именам колонок: колонка `housing_complex_id` в таблице apartments
    указывает на таблицу housing_complexes.
    """
    specs = specs or {}
    tables = {t["name"]: t for t in dataops.list_tables(db_path)}
    columns = {}
    id_fields = {}
    for name in tables:
        try:
            cols = dataops.columns(db_path, name)
        except dataops.DataError:
            continue
        columns[name] = [c["name"] for c in cols]
        spec = specs.get(name)
        if spec and spec.id_field in columns[name]:
            id_fields[name] = spec.id_field
        elif "id" in columns[name]:
            id_fields[name] = "id"

    relations, seen = [], set()

    # 1. явные связи из описаний источников
    for key, spec in specs.items():
        parent = spec.split_values_from
        if not parent or key not in columns or parent not in columns:
            continue
        child_col = None
        # split_param вида filter[housing_complex_id][] -> housing_complex_id
        match = re.findall(r"[\w.]+", spec.split_param or "")
        for candidate in reversed(match):
            if candidate in columns[key]:
                child_col = candidate
                break
        if child_col and (key, child_col, parent) not in seen:
            seen.add((key, child_col, parent))
            relations.append({"child": key, "child_field": child_col,
                              "parent": parent,
                              "parent_field": spec.split_values_field
                              or id_fields.get(parent, "id"),
                              "source": "описание источника"})

    # 2. связи, угаданные по именам колонок
    for child, cols in columns.items():
        for col in cols:
            if not col.endswith("_id") or col == id_fields.get(child):
                continue
            stem = col[:-3]
            for parent in columns:
                if parent == child:
                    continue
                if _singularize(parent) == stem or parent == stem:
                    key = (child, col, parent)
                    if key in seen:
                        continue
                    seen.add(key)
                    relations.append({"child": child, "child_field": col,
                                      "parent": parent,
                                      "parent_field": id_fields.get(parent, "id"),
                                      "source": "имя колонки"})
    return relations


def run(db_path, specs=None, last_run=None, sample=5) -> dict:
    """Выполняет все проверки. Возвращает сводку и список замечаний."""
    specs = specs or {}
    issues = []
    tables = dataops.list_tables(db_path)
    if not tables:
        return {"issues": [_issue(INFO, "данные", "Данных нет",
                                  "Соберите хотя бы один источник.")],
                "relations": [], "tables": []}

    with dataops.connect_ro(db_path) as conn:
        _check_completeness(conn, issues, tables, specs, last_run)
        relations = detect_relations(db_path, specs)
        _check_relations(conn, issues, relations, sample, specs)
        _check_duplicates(conn, issues, tables, specs)
        _check_required(conn, issues, tables, specs)
        _check_positive(conn, issues, tables, specs)
        _check_name_consistency(conn, issues, relations, specs)

    order = {CRITICAL: 0, WARNING: 1, INFO: 2}
    issues.sort(key=lambda i: order[i["level"]])
    counts = {level: sum(1 for i in issues if i["level"] == level)
              for level in (CRITICAL, WARNING, INFO)}
    return {"issues": issues, "counts": counts, "relations": relations,
            "tables": tables,
            "verdict": _verdict(counts)}


def _verdict(counts):
    if counts.get(CRITICAL):
        return ("critical", "Данные неполные или несогласованные — "
                            "на них нельзя опираться без разбора причин.")
    if counts.get(WARNING):
        return ("warning", "Данные пригодны, но есть места, которые стоит "
                           "проверить перед серьёзными выводами.")
    return ("ok", "Сквозные проверки пройдены: таблицы сходятся между собой.")


def _count(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()[0]


# ──────────────────────────────────────────────────────────────
def _check_completeness(conn, issues, tables, specs, last_run):
    """Собрано против заявленного API. Главная проверка: именно здесь
    видно, что источник недобрал записи."""
    if not last_run:
        issues.append(_issue(INFO, "полнота", "История запусков пуста",
                             "Сверить собранное с заявленным API объёмом нечем.",
                             advice="Запустите сбор — метрики запишутся сами."))
        return

    reported = last_run.get("region_total_reported")
    collected = last_run.get("apartments_count")
    if reported and collected is not None:
        share = 100.0 * collected / reported
        if share < 80:
            issues.append(_issue(
                CRITICAL, "полнота",
                f"Собрано {collected} из {reported} записей ({share:.1f}%)",
                f"Потеряно {reported - collected} записей главного источника.",
                number=reported - collected,
                advice="Запустите: python -m redcat.collection.redcat_scraper --diagnose — "
                       "проверка покажет, на каком шаге теряются записи."))
        elif share < 99:
            issues.append(_issue(
                WARNING, "полнота",
                f"Собрано {collected} из {reported} ({share:.1f}%)",
                "Небольшая недостача — обычно это записи, привязанные к "
                "значениям вне справочника.",
                number=reported - collected))
        else:
            issues.append(_issue(INFO, "полнота",
                                 f"Полнота сбора {share:.1f}%",
                                 f"{collected} из {reported}"))
    elif collected:
        issues.append(_issue(
            WARNING, "полнота", "Общее число записей от API неизвестно",
            "Полноту сбора не с чем сверять — недостача останется незамеченной.",
            advice='Найдите total в ответе API и укажите его в "total_path".'))

    if last_run.get("incomplete"):
        issues.append(_issue(
            CRITICAL, "полнота", "Последний запуск помечен как неполный",
            "Часть данных не догрузилась — показатели занижены.",
            advice="Смотрите redcat_scraper.log и повторите сбор."))


def _check_relations(conn, issues, relations, sample, specs=None):
    """Битые ссылки и покрытие справочника."""
    for rel in relations:
        child, cf = rel["child"], rel["child_field"]
        parent, pf = rel["parent"], rel["parent_field"]
        try:
            orphans = _count(conn, f'''
                SELECT COUNT(*) FROM "{child}" c
                WHERE c."{cf}" IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM "{parent}" p
                                  WHERE CAST(p."{pf}" AS TEXT) = CAST(c."{cf}" AS TEXT))''')
            child_total = _count(conn, f'SELECT COUNT(*) FROM "{child}"')
            parent_total = _count(conn, f'SELECT COUNT(*) FROM "{parent}"')
            used = _count(conn, f'''
                SELECT COUNT(*) FROM "{parent}" p
                WHERE EXISTS (SELECT 1 FROM "{child}" c
                              WHERE CAST(c."{cf}" AS TEXT) = CAST(p."{pf}" AS TEXT))''')
        except sqlite3.Error:
            continue

        if orphans:
            share = 100.0 * orphans / child_total if child_total else 0
            issues.append(_issue(
                CRITICAL if share > 5 else WARNING, "связи",
                f"«{child}»: {orphans} записей ссылаются на «{parent}», "
                f"которых нет в справочнике",
                f"Поле {cf} → {parent}.{pf}. Это {share:.1f}% таблицы.",
                number=orphans,
                advice=f"Справочник «{parent}» собран не полностью либо отфильтрован "
                       f"строже дочерней таблицы. Сверьте фильтры в URL обоих источников."))

        if parent_total:
            empty = parent_total - used
            coverage = 100.0 * used / parent_total
            child_spec = (specs or {}).get(child)
            if child_spec is not None and getattr(child_spec, "max_records", None):
                # срез (max_records): низкое покрытие справочника — настройка, а не поломка
                issues.append(_issue(
                    INFO, "покрытие",
                    f"«{parent}» → «{child}»: в срезе представлено {used} из {parent_total} "
                    f"({coverage:.1f}%)",
                    f"«{child}» собирается срезом (max_records={child_spec.max_records}); "
                    f"{empty} записей «{parent}» в него не попали — ожидаемо."))
            elif coverage < 50:
                issues.append(_issue(
                    CRITICAL, "покрытие",
                    f"У {empty} из {parent_total} записей «{parent}» "
                    f"нет ни одной связанной записи в «{child}»",
                    f"Покрытие всего {coverage:.1f}%.",
                    number=empty,
                    advice=f"Это типичный след неполного сбора «{child}»: "
                           f"дробление по «{parent}» отработало лишь частично. "
                           f"Проверьте: python -m redcat.collection.redcat_scraper --diagnose {child}"))
            elif coverage < 90:
                issues.append(_issue(
                    WARNING, "покрытие",
                    f"{empty} записей «{parent}» без связанных в «{child}» "
                    f"(покрытие {coverage:.1f}%)",
                    "Может быть нормой (ЖК без лотов в продаже), "
                    "но стоит выборочно проверить.",
                    number=empty))
            else:
                issues.append(_issue(
                    INFO, "покрытие",
                    f"«{parent}» → «{child}»: покрытие {coverage:.1f}%",
                    f"{used} из {parent_total}"))


def _check_duplicates(conn, issues, tables, specs):
    for t in tables:
        name = t["name"]
        spec = specs.get(name)
        id_field = spec.id_field if spec else "id"
        try:
            dup = _count(conn, f'''
                SELECT COUNT(*) FROM (SELECT "{id_field}" FROM "{name}"
                WHERE "{id_field}" IS NOT NULL
                GROUP BY "{id_field}" HAVING COUNT(*) > 1)''')
        except sqlite3.Error:
            continue
        if dup:
            issues.append(_issue(
                CRITICAL, "дубликаты",
                f"«{name}»: {dup} идентификаторов встречаются больше одного раза",
                f"Поле {id_field}. Любой подсчёт по этой таблице завышен.",
                number=dup,
                advice="Частая причина — дробление запроса по параметру, который "
                       "API игнорирует: одна и та же выдача приходит много раз."))


def _check_required(conn, issues, tables, specs):
    for t in tables:
        name = t["name"]
        spec = specs.get(name)
        if not spec or not spec.required_fields:
            continue
        for field in spec.required_fields:
            try:
                empty = _count(conn, f'''
                    SELECT COUNT(*) FROM "{name}"
                    WHERE "{field}" IS NULL OR TRIM(CAST("{field}" AS TEXT)) = ''')
            except sqlite3.Error:
                continue
            if empty:
                share = 100.0 * empty / max(t["rows"], 1)
                issues.append(_issue(
                    CRITICAL if share > 10 else WARNING, "обязательные поля",
                    f"«{name}»: у {empty} записей пусто обязательное поле «{field}»",
                    f"Это {share:.1f}% таблицы.", number=empty))


def _check_positive(conn, issues, tables, specs):
    for t in tables:
        name = t["name"]
        spec = specs.get(name)
        if not spec or not spec.positive_fields:
            continue
        for field in spec.positive_fields:
            try:
                bad = _count(conn, f'''
                    SELECT COUNT(*) FROM "{name}"
                    WHERE CAST("{field}" AS REAL) <= 0 AND "{field}" IS NOT NULL''')
            except sqlite3.Error:
                continue
            if bad:
                share = 100.0 * bad / max(t["rows"], 1)
                issues.append(_issue(
                    WARNING if share < 5 else CRITICAL, "невозможные значения",
                    f"«{name}»: {bad} записей имеют «{field}» ≤ 0",
                    f"Это {share:.1f}% таблицы. Средние и суммы по полю смещены.",
                    number=bad,
                    advice=f"Исключайте такие строки фильтром «{field} больше 0» "
                           f"либо поправьте их в локальной копии."))


def _check_name_consistency(conn, issues, relations, specs):
    """Одна сущность — разные названия в разных таблицах."""
    for rel in relations:
        child, cf = rel["child"], rel["child_field"]
        parent, pf = rel["parent"], rel["parent_field"]
        parent_spec = specs.get(parent)
        parent_name = parent_spec.name_field if parent_spec else "name"
        # в дочерней таблице ищем колонку с продублированным названием
        child_name = f"{_singularize(parent)}_name"
        try:
            mismatched = _count(conn, f'''
                SELECT COUNT(*) FROM "{child}" c JOIN "{parent}" p
                  ON CAST(p."{pf}" AS TEXT) = CAST(c."{cf}" AS TEXT)
                WHERE c."{child_name}" IS NOT NULL AND p."{parent_name}" IS NOT NULL
                  AND TRIM(c."{child_name}") <> TRIM(p."{parent_name}")''')
        except sqlite3.Error:
            continue
        if mismatched:
            issues.append(_issue(
                WARNING, "рассогласование",
                f"У {mismatched} записей «{child}» название не совпадает "
                f"с названием в «{parent}»",
                f"{child}.{child_name} ≠ {parent}.{parent_name}",
                number=mismatched,
                advice="Обычно означает, что таблицы собраны в разное время. "
                       "Пересоберите оба источника в одном запуске."))
