"""
ГЕНЕРАТОР РУССКИХ ПОДПИСЕЙ ДЛЯ КОЛОНОК
======================================
Читает схему из reports/redcat_data.db, разбирает каждое имя колонки на
токены, переводит их по словарю и пишет field_labels.py.

Что делает:
  1. Читает все таблицы и колонки (кроме служебных).
  2. Переводит имена: housing_complex_id → "ID ЖК", price_per_sqm → "Цена за м², ₽".
  3. Существующий field_labels.py подхватывает — ручные правки не теряются.
  4. Пишет черновик с пометкой «проверить» там, где перевёл не все токены.

Запуск:
    python make_field_labels.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DB = BASE_DIR / "reports" / "redcat_data.db"
OUT_FILE = BASE_DIR / "field_labels.py"

# Служебные таблицы — их колонки переводить не надо.
SKIP_TABLES = {"sqlite_sequence", "comparison_vs_previous"}


# ──────────────────────────────────────────────────────────────
#  Словарь токенов. Ключ — кусок имени (регистр не важен), значение — подпись.
#  Порядок неважен: скрипт сам сортирует по длине (сначала длинные).
# ──────────────────────────────────────────────────────────────
TOKENS = {
    # сущности
    "housing_complex": "ЖК",
    "complex": "ЖК",
    "hc": "ЖК",
    "developer": "Застройщик",
    "provider": "Провайдер",
    "district": "Район",
    "region": "Регион",
    "address": "Адрес",
    "building": "Корпус",
    "apartment": "Квартира",
    "apartments": "Квартира",
    "lot": "Лот",
    "object": "Объект",
    "flat": "Квартира",
    "regulation": "Регламент",
    "tariff": "Тариф",

    # поля
    "id": "ID",
    "name": "Название",
    "title": "Название",
    "type": "Тип",
    "status": "Статус",
    "slug": "Слаг",
    "url": "URL",
    "image": "Фото",
    "photo": "Фото",
    "plan": "Планировка",

    # числа
    "price": "Цена",
    "cost": "Стоимость",
    "area": "Площадь",
    "total": "Общая",
    "min": "Мин.",
    "max": "Макс.",
    "count": "Количество",
    "number": "Номер",
    "floor": "Этаж",
    "floors": "Этажей",
    "room": "Комнат",
    "rooms": "Комнат",
    "term": "Срок",
    "commission": "Комиссия",
    "percent": "%",
    "pct": "%",
    "rate": "Ставка",
    "payment": "Платёж",
    "land": "Участок",
    "buildings": "Корпусов",
    "deadline": "Срок сдачи",
    "delivery": "Сдача",
    "date": "Дата",
    "created": "Дата появления",
    "updated": "Дата обновления",
    "from": "с",
    "to": "до",
    "is": "",
    "active": "в продаже",
    "dynamic": "динамика",
    "dynamics": "динамика",

    # служебные суффиксы
    "per": "за",
    "sqm": "м²",
    "sq": "м²",
    "m2": "м²",
    "real": "",
    "int": "",
    "text": "",
}


# Единицы измерения по имени колонки — приписываются в конце подписи.
# Порядок важен: срабатывает первое совпадение.
UNIT_RULES = [
    ("price_per_sqm", ", ₽/м²"),
    ("price_per_m2", ", ₽/м²"),
    ("per_sqm", ", ₽/м²"),
    ("commission_percent", ", %"),
    ("min_percent", ", %"),
    ("max_percent", ", %"),
    ("percent", ", %"),
    ("_pct", ", %"),
    ("price", ", ₽"),
    ("_area", ", м²"),
    ("area", ", м²"),
    ("payment_term", ", мес."),
    ("term", ", мес."),
    ("buildings_count", ", шт."),
    ("floors_total", ", шт."),
    ("floor", ", эт."),
    ("rooms", ", шт."),
    ("room", ", шт."),
]


def read_schema(db_path: Path) -> dict[str, list[str]]:
    """Возвращает {таблица: [колонка, ...]} для всех таблиц базы."""
    schema: dict[str, list[str]] = {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
            if r[0] not in SKIP_TABLES and not r[0].startswith("sqlite_")
        ]
        for t in tables:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
            schema[t] = cols
    finally:
        conn.close()
    return schema


def _split_tokens(field: str) -> list[str]:
    """Разбивает имя колонки на токены по '_', '.', ' '.

    Пример: housing_complex.url_slug → ['housing_complex', 'url', 'slug'].
    Длинные токены идут первыми — иначе housing_complex_id распался бы на
    ['housing', 'complex', 'id'] и перевёлся бы как «Жилищный Комплекс ID».
    """
    parts = field.replace(".", "_").replace(" ", "_").split("_")
    return [p.lower() for p in parts if p]


def translate(field: str) -> tuple[str, bool]:
    """Переводит имя колонки. Возвращает (подпись, уверен_ли_в_переводе).

    Сначала пробуем найти совпадение целиком. Потом разбираем на токены.
    Если хоть один токен неизвестен — возвращаем False во втором элементе,
    чтобы человек проверил перевод.
    """
    # 1. Особые случаи, которые неудобно выражать токенами.
    special = {
        "price_per_sqm": "Цена за м²",
        "price_per_m2": "Цена за м²",
        "min_price_apartments": "Мин. цена квартиры",
        "total_area": "Площадь общая",
        "floor": "Этаж",
        "rooms": "Комнат",
        "floors_total": "Этажей в доме",
        "total_floor": "Этажность дома",
        "housing_complex.url_slug": "URL-слаг ЖК",
        "housing_complex.slug": "Слаг ЖК",
        "housing_complex.deadline": "Срок сдачи ЖК",
        "flat_plan_image": "Планировка",
        "lot_price_dynamics": "Динамика цены",
        "estate_delivery_date": "Срок сдачи",
        "created_at": "Дата появления",
        "is_active": "В продаже",
        "run_id": "Номер запуска",
    }
    if field in special:
        return _with_unit(field, special[field]), True

    # 2. Токенизация.
    tokens = _split_tokens(field)

    # Отбрасываем пустые служебные токены (например 'real', 'int').
    tokens = [t for t in tokens if t and TOKENS.get(t, t) != ""]

    # Если остался всего один токен — берём его перевод или имя как есть.
    if len(tokens) == 1:
        t = tokens[0]
        if t in TOKENS and TOKENS[t]:
            return _with_unit(field, TOKENS[t]), True
        if t in TOKENS and TOKENS[t] == "":
            return _with_unit(field, field), True
        return field, False

    # 3. Собираем фразу. Особая логика для «id» — он всегда в начале с сущностью.
    words: list[str] = []
    confident = True
    for t in tokens:
        if t in TOKENS:
            w = TOKENS[t]
            if w:
                words.append(w)
        else:
            # неизвестный токен — оставляем как есть, помечаем неуверенность
            words.append(t)
            confident = False

    # Если последний токен 'id' — переносим его в начало с предыдущим словом.
    if tokens and tokens[-1] == "id" and len(words) >= 2:
        # ['ЖК', 'ID'] → 'ID ЖК'
        id_word = words[-1]
        prev = words[-2]
        words = [f"{id_word} {prev}"] + words[:-2]

    label = " ".join(words)
    return _with_unit(field, label), confident


def _with_unit(field: str, label: str) -> str:
    """Приписывает единицу измерения, если её ещё нет в подписи."""
    for needle, unit in UNIT_RULES:
        if needle in field:
            if unit.strip(", ") in label:
                return label
            return label + unit
    return label


def load_existing() -> dict[str, str]:
    """Читает существующий field_labels.py, если он уже есть.

    Нужно, чтобы повторный запуск не затирал ручные правки. Импортируем
    файл как модуль и берём из него FIELD_LABELS.
    """
    if not OUT_FILE.exists():
        return {}
    import importlib.util
    spec = importlib.util.spec_from_file_location("_existing_labels", OUT_FILE)
    if spec is None or spec.loader is None:
        return {}
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return dict(getattr(module, "FIELD_LABELS", {}))
    except Exception as e:
        print(f"⚠️ Не удалось прочитать существующий field_labels.py: {e}")
        return {}


def write_labels(schema: dict[str, list[str]], existing: dict[str, str]) -> None:
    """Пишет field_labels.py: группирует по таблицам, сохраняет старые правки."""
    lines: list[str] = []
    lines.append('"""')
    lines.append("Русские подписи для полей базы.")
    lines.append("=================================")
    lines.append("")
    lines.append("Сгенерировано скриптом make_field_labels.py.")
    lines.append("Правьте вручную: при следующем запуске скрипта ваши строки")
    lines.append("сохранятся, а новые поля добавятся автоматически.")
    lines.append("")
    lines.append("Поля, у которых стоит пометка «# проверить», переведены")
    lines.append("не полностью — гляньте на них глазами.")
    lines.append('"""')
    lines.append("")
    lines.append("FIELD_LABELS: dict[str, str] = {")
    lines.append("")

    stats = {"confident": 0, "doubt": 0, "kept": 0}

    for table, columns in schema.items():
        lines.append(f"    # ── {table} ──")
        for col in columns:
            # ручная правка имеет приоритет над автопереводом
            if col in existing:
                label = existing[col]
                stats["kept"] += 1
                lines.append(f'    {col!r}: {label!r},')
                continue

            label, confident = translate(col)
            if confident:
                stats["confident"] += 1
                lines.append(f'    {col!r}: {label!r},')
            else:
                stats["doubt"] += 1
                lines.append(f'    {col!r}: {label!r},  # проверить')
        lines.append("")

    # сохранённые строки, которых нет в текущей схеме — тоже оставим,
    # они могут понадобиться для других таблиц
    extra = {k: v for k, v in existing.items()
             if k not in {c for cols in schema.values() for c in cols}}
    if extra:
        lines.append("    # ── прочие поля (не найдены в текущей схеме) ──")
        for k, v in sorted(extra.items()):
            lines.append(f'    {k!r}: {v!r},')
        lines.append("")

    lines.append("}")
    lines.append("")
    lines.append("")
    lines.append("def label_for(field: str) -> str:")
    lines.append('    """Русская подпись для поля или исходное имя, если перевода нет."""')
    lines.append("    return FIELD_LABELS.get(field, field)")
    lines.append("")
    lines.append("")
    lines.append("def apply_to_columns(columns: list) -> list:")
    lines.append('    """Добавляет каждому элементу columns ключ label."""')
    lines.append("    for c in columns:")
    lines.append('        c["label"] = label_for(c["name"])')
    lines.append("    return columns")
    lines.append("")

    OUT_FILE.write_text("\n".join(lines), encoding="utf-8")

    print(f"✅ Записано: {OUT_FILE.name}")
    print(f"   Уверенных переводов:   {stats['confident']}")
    print(f"   Требуют проверки:      {stats['doubt']}  (помечены «# проверить»)")
    print(f"   Сохранено ручных:      {stats['kept']}")


def main() -> int:
    if not DATA_DB.exists():
        print(f"❌ База не найдена: {DATA_DB}")
        print("   Соберите данные или создайте демо-набор: python demo_data.py")
        return 1

    schema = read_schema(DATA_DB)
    if not schema:
        print("❌ В базе нет ни одной таблицы.")
        return 1

    total_cols = sum(len(c) for c in schema.values())
    print(f"📖 Прочитано таблиц: {len(schema)}, колонок: {total_cols}")

    existing = load_existing()
    if existing:
        print(f"💾 Найдены существующие подписи: {len(existing)} — сохранятся")

    write_labels(schema, existing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())