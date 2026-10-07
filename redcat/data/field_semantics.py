"""
Семантический словарь имён полей.
==================================
По имени и значению колонки определяет её категорию (id, name, price,
area, floor, rooms, status, developer, complex и т.д.), уверенность
классификации, а также предлагает derived-формулы и правила проверки.

Данные — в field_semantics.json рядом с модулем. Читается на лету,
кэш сбрасывается по mtime.

Голосование по записям
----------------------
suggest_spec_fields принимает одну запись (dict) или список записей
(список dict). Когда записей несколько, классификатор считает по каждой
колонке fill_rate (долю непустых) и type_consistency (долю доминирующего
типа) — это делает угадывание устойчивым к единичным null и «шумным»
значениям в первой записи.

Публичный API:
  normalize_name(field)         — нормализованное имя (для сравнений)
  classify(field)               — категория по имени или None
  classify_full(field, value)   — категория + уверенность + источник
  roles_of(category)            — набор ролей
  priority_of(category)         — приоритет
  analyze_record(flat)          — разбор всех полей одной записи
  suggest_spec_fields(flats)    — набор полей для SourceSpec
  suggest_derived(flat)         — предложения вычисляемых колонок
  suggest_checks(spec_fields)   — предложения проверок данных
"""
from __future__ import annotations

from redcat.core import paths
import json
import re
from pathlib import Path

_FILE = paths.CONFIG_DIR / "field_semantics.json"
_cache: dict | None = None
_mtime: float = 0.0


# ──────────────────────────────────────────────────────────────
#  Загрузка
# ──────────────────────────────────────────────────────────────
def _load_full() -> dict:
    global _cache, _mtime
    try:
        cur = _FILE.stat().st_mtime
    except OSError:
        cur = 0.0
    if _cache is None or cur != _mtime:
        try:
            data = json.loads(_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        _cache = data
        _mtime = cur
    return _cache or {}


def _load() -> dict:
    return _load_full().get("categories") or {}


def _derived_rules() -> list:
    return _load_full().get("_derived_rules") or []


def _check_rules() -> list:
    return _load_full().get("_checks_rules") or []


# ──────────────────────────────────────────────────────────────
#  Нормализация имени
# ──────────────────────────────────────────────────────────────
_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})


def normalize_name(field: str) -> str:
    """Нормализует имя поля: camelCase → snake, транслит, единый регистр."""
    if not field:
        return ""
    s = str(field).strip()
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = s.lower().translate(_TRANSLIT)
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


# ──────────────────────────────────────────────────────────────
#  Индекс синонимов
# ──────────────────────────────────────────────────────────────
def _index() -> dict:
    idx: dict = {}
    for cat, info in _load().items():
        pr = int(info.get("priority", 0))
        for syn in info.get("canonical", []):
            key = normalize_name(syn)
            if not key:
                continue
            if key in idx and idx[key][1] >= pr:
                continue
            idx[key] = (cat, pr)
    return idx


# ──────────────────────────────────────────────────────────────
#  Типы значений
# ──────────────────────────────────────────────────────────────
def _is_number(v) -> bool:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return True
    if isinstance(v, str):
        try:
            float(v.replace(" ", "").replace("\xa0", "").replace(",", "."))
            return True
        except (ValueError, AttributeError):
            return False
    return False


def _to_float(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(" ", "").replace("\xa0", "").replace(",", "."))
        except (ValueError, AttributeError):
            return None
    return None


def _value_type(v) -> str:
    if v is None:
        return "empty"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "object"
    s = str(v).strip()
    if not s:
        return "empty"
    if re.match(r"^https?://", s):
        return "url"
    if re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", s):
        return "email"
    if re.match(r"^\+?[\d\s\-()]{7,}$", s) and any(c.isdigit() for c in s):
        return "phone"
    if re.match(r"^\d{4}-\d{2}-\d{2}|^\d{2}\.\d{2}\.\d{4}|^\d{2}/\d{2}/\d{4}", s):
        return "date"
    if _is_number(s):
        return "numeric_string"
    if s.lower() in ("true", "false", "yes", "no", "да", "нет"):
        return "bool_string"
    return "string"


# ──────────────────────────────────────────────────────────────
#  Классификация поля
# ──────────────────────────────────────────────────────────────
def detect_by_value(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    best_cat, best_prio = None, -1
    for cat, info in _load().items():
        pat = info.get("value_hint")
        if not pat:
            continue
        try:
            if re.match(pat, s):
                pr = int(info.get("priority", 0))
                if pr > best_prio:
                    best_cat, best_prio = cat, pr
        except re.error:
            continue
    return best_cat


def classify(field: str) -> str | None:
    return classify_full(field, None)[0]


def classify_full(field: str, value=None) -> tuple:
    """Возвращает (category, confidence, source).

    source ∈ {"exact", "suffix", "substring", "value", "none"}.
    """
    n = normalize_name(field)

    if n:
        idx = _index()
        if n in idx:
            return idx[n][0], 0.95, "exact"

    if n.endswith("_id") or n.endswith("_ids"):
        return "foreign_id", 0.75, "suffix"

    if n:
        idx = _index()
        best_cat, best_prio, best_len = None, -1, 0
        for syn, (cat, pr) in idx.items():
            if len(syn) < 3:
                continue
            if n.endswith("_" + syn) or n == syn:
                if pr > best_prio or (pr == best_prio and len(syn) > best_len):
                    best_cat, best_prio, best_len = cat, pr, len(syn)
        if best_cat is not None:
            return best_cat, 0.75, "suffix"

    if n:
        idx = _index()
        for syn, (cat, pr) in idx.items():
            if len(syn) >= 5 and ("_" + syn + "_") in ("_" + n + "_"):
                return cat, 0.6, "substring"

    by_value = detect_by_value(value) if value is not None else None
    if by_value:
        return by_value, 0.7, "value"

    return None, 0.0, "none"


def roles_of(category: str) -> set:
    info = _load().get(category) or {}
    return set(info.get("roles") or [])


def priority_of(category: str) -> int:
    info = _load().get(category) or {}
    return int(info.get("priority", 0))


def value_range_of(category: str):
    info = _load().get(category) or {}
    return info.get("value_range")


# ──────────────────────────────────────────────────────────────
#  Анализ одной записи (для UI)
# ──────────────────────────────────────────────────────────────
def analyze_record(flat: dict) -> dict:
    """Разбор одной нормализованной записи.

    Для каждого поля: category, confidence, source, value_type, roles, usage.
    """
    out: dict = {}
    if not isinstance(flat, dict):
        return out
    for field, value in flat.items():
        cat, conf, src = classify_full(field, value)
        vtype = _value_type(value)
        roles = roles_of(cat) if cat else set()
        if "ignore" in roles:
            usage = None
        elif "id_field" in roles and conf >= 0.7:
            usage = "id_field"
        elif "name_field" in roles and conf >= 0.7:
            usage = "name_field"
        elif "numeric" in roles and vtype in ("number", "numeric_string"):
            usage = "numeric_fields"
        elif "group" in roles:
            usage = "group_fields"
        elif "date" in roles:
            usage = "track_fields"
        elif "track" in roles:
            usage = "track_fields"
        else:
            usage = None
        out[field] = {
            "category": cat,
            "confidence": round(conf, 3),
            "source": src,
            "value_type": vtype,
            "roles": sorted(roles),
            "usage": usage,
        }
    return out


# ──────────────────────────────────────────────────────────────
#  Голосование по записям
# ──────────────────────────────────────────────────────────────
def _as_flats(flat_or_flats, max_records: int = 50) -> list:
    """Приводит вход к списку плоских записей."""
    if isinstance(flat_or_flats, list):
        items = [f for f in flat_or_flats[:max_records] if isinstance(f, dict)]
    elif isinstance(flat_or_flats, dict):
        items = [flat_or_flats]
    else:
        items = []
    return items


def _aggregate_fields(flats: list) -> dict:
    """{field: {total, filled, types, sample_values, numeric_values}}."""
    agg: dict = {}
    for flat in flats:
        for field, value in flat.items():
            a = agg.setdefault(field, {
                "total": 0, "filled": 0,
                "types": {},
                "sample_values": [],
                "numeric_values": [],
            })
            a["total"] += 1
            if value in (None, ""):
                continue
            a["filled"] += 1
            t = _value_type(value)
            a["types"][t] = a["types"].get(t, 0) + 1
            if len(a["sample_values"]) < 5:
                a["sample_values"].append(value)
            n = _to_float(value)
            if n is not None and not isinstance(value, bool):
                if len(a["numeric_values"]) < 50:
                    a["numeric_values"].append(n)
    return agg


def _field_signals(a: dict) -> dict:
    total = a["total"] or 1
    fill_rate = a["filled"] / total
    types = a["types"]
    total_typed = sum(types.values())
    if total_typed == 0:
        dominant_type, type_consistency = "empty", 0.0
    else:
        dominant_type, dom_n = max(types.items(), key=lambda kv: kv[1])
        type_consistency = dom_n / total_typed
    return {
        "fill_rate": round(fill_rate, 3),
        "dominant_type": dominant_type,
        "type_consistency": round(type_consistency, 3),
    }


def _range_score(category: str, values: list) -> float:
    """Доля значений в value_range категории. 1.0 — все в диапазоне,
    0.0 — ни одно. Если value_range не задан — возвращаем 1.0 (нейтрально).
    """
    rng = value_range_of(category)
    if not rng or not values:
        return 1.0
    try:
        lo, hi = float(rng[0]), float(rng[1])
    except (TypeError, ValueError, IndexError):
        return 1.0
    inside = sum(1 for v in values if lo <= v <= hi)
    return inside / len(values)


# ──────────────────────────────────────────────────────────────
#  Display-варианты
# ──────────────────────────────────────────────────────────────
_DISPLAY_SUFFIXES = ("_formatted", "_str", "_string", "_text",
                     "_display", "_human", "_label", "_pretty",
                     "_formatted_value", "_formatted_text")


def _is_display_variant(field: str) -> bool:
    """price_formatted, price.string, price_str — текстовое представление.

    Такие поля не идут в numeric_fields, даже если иногда содержат цифры.
    """
    n = normalize_name(field)
    for sfx in _DISPLAY_SUFFIXES:
        if n.endswith(sfx):
            return True
    return False


# ──────────────────────────────────────────────────────────────
#  Сборка полей spec
# ──────────────────────────────────────────────────────────────
def suggest_spec_fields(flat_or_flats, max_numeric: int = 20,
                        max_records: int = 50) -> dict:
    """Собирает поля spec, голосуя по записям.

    Принимает одну запись (dict) или список записей. Список даёт более
    устойчивый результат: если в одной записи price=null, а в остальных
    числа, поле всё равно попадёт в numeric_fields.
    """
    flats = _as_flats(flat_or_flats, max_records)
    if not flats:
        return {}

    agg = _aggregate_fields(flats)

    # Классификация + сигналы
    classified: dict = {}
    for field, a in agg.items():
        signals = _field_signals(a)
        sample_val = a["sample_values"][0] if a["sample_values"] else None
        cat, conf, src = classify_full(field, sample_val)
        if cat is None:
            continue
        roles = roles_of(cat)
        if "ignore" in roles:
            continue
        is_display = _is_display_variant(field)
        rscore = 1.0
        if "numeric" in roles and a["numeric_values"]:
            rscore = _range_score(cat, a["numeric_values"])
        classified[field] = {
            "field": field, "cat": cat, "conf": conf, "src": src,
            "roles": roles, "signals": signals,
            "is_display": is_display, "range_score": rscore,
        }

    # id_field — самый уверенный из категории id с высоким fill_rate
    id_candidates = [c for c in classified.values()
                     if "id_field" in c["roles"] and not c["is_display"]]
    id_field = _pick_top(id_candidates) or "id"

    # name_field
    name_candidates = [c for c in classified.values()
                       if "name_field" in c["roles"]
                       and c["field"] != id_field
                       and not c["is_display"]]
    name_field = _pick_top(name_candidates) or id_field

    # numeric_fields
    numeric_candidates = []
    for c in classified.values():
        if c["field"] == id_field:
            continue
        if "numeric" not in c["roles"]:
            continue
        if c["is_display"]:
            continue
        s = c["signals"]
        if s["fill_rate"] < 0.2:
            continue
        if s["dominant_type"] not in ("number", "numeric_string"):
            continue
        if s["type_consistency"] < 0.7:
            continue
        if c["range_score"] < 0.5:
            continue
        numeric_candidates.append(c)

    # Сортируем: приоритет категории × уверенность × fill_rate; при равенстве
    # предпочитаем поле без «формы» (price лучше, чем price_with_discount).
    def num_key(c):
        n = normalize_name(c["field"])
        priority = priority_of(c["cat"])
        # Категория совпадает с самым коротким синонимом — бонус
        canonical_exact = 0 if c["src"] == "exact" else 1
        return (-(priority * c["conf"] * c["signals"]["fill_rate"]),
                canonical_exact, len(n), n)
    numeric_candidates.sort(key=num_key)
    numeric = [c["field"] for c in numeric_candidates[:max_numeric]]

    # Дедуп по базовой категории: price.value и price.amount — одно поле
    numeric = _dedup_by_base(numeric)

    positive = [f for f in numeric
                if "positive" in roles_of(classify(f) or "")]

    # group_fields
    group_candidates = []
    for c in classified.values():
        if "group" not in c["roles"] or c["is_display"]:
            continue
        s = c["signals"]
        if s["fill_rate"] < 0.1:
            continue
        group_candidates.append(c)
    group_candidates.sort(
        key=lambda c: (-priority_of(c["cat"]), c["field"]))
    groups: list = []
    for c in group_candidates:
        if c["field"] not in groups:
            groups.append(c["field"])

    # track_fields: name + numeric + track-роли
    track: list = []
    if name_field and name_field != id_field:
        track.append(name_field)
    for f in numeric:
        if f not in track:
            track.append(f)
    for c in classified.values():
        if "track" not in c["roles"] or c["field"] == id_field:
            continue
        if c["field"] not in track:
            track.append(c["field"])
    track = track[:15]

    # required: id + name + positive
    required: list = [id_field]
    if name_field and name_field != id_field:
        required.append(name_field)
    for f in positive:
        if f not in required:
            required.append(f)

    # date
    dates = [c["field"] for c in classified.values()
             if "date" in c["roles"]]

    derived = suggest_derived(flats[0])

    warnings: list = []
    if id_field == "id" and "id" not in agg:
        warnings.append({
            "level": "warning", "field": "id_field",
            "message": "в записях нет поля, похожего на первичный ключ",
            "advice": "Проверьте ответ — возможно, id называется иначе "
                      "или лежит во вложенном объекте.",
        })
    if not numeric:
        warnings.append({
            "level": "info", "field": "numeric_fields",
            "message": "числовых полей не найдено",
            "advice": "Если у источника есть цены и площади — проверьте, "
                      "что ответ их отдаёт в первых записях.",
        })

    out = {
        "id_field": id_field,
        "name_field": name_field,
        "numeric_fields": numeric,
        "positive_fields": positive,
        "group_fields": groups,
        "track_fields": track,
        "required_fields": required,
    }
    if dates:
        out["date_fields"] = dates
    if derived:
        out["derived"] = derived
    if warnings:
        out["warnings"] = warnings
    return out


def _pick_top(candidates: list):
    """Из списка кандидатов выбирает самый «весомый» по вкладу в spec."""
    if not candidates:
        return None

    def key(c):
        n = normalize_name(c["field"])
        return (-priority_of(c["cat"]) * c["conf"],
                c["signals"]["fill_rate"] * -1,
                0 if c["src"] == "exact" else 1,
                len(n), n)
    return sorted(candidates, key=key)[0]["field"]


def _base_of(field: str) -> str:
    """Базовая часть имени: price.value → price, price_formatted → price,
    total_area → area, housing_complex_id → housing_complex.

    Используется для дедупликации: если два поля отображаются в одну
    категорию и один — префикс другого, оставляем более короткое имя.
    """
    n = normalize_name(field)
    for cut in ("_formatted", "_str", "_string", "_text",
                "_display", "_human", "_label", "_pretty",
                ".value", ".amount", ".total", "_value", "_amount"):
        if n.endswith(cut):
            n = n[:-len(cut)]
    return n


def _dedup_by_base(fields: list) -> list:
    """Убирает дубликаты, у которых базовая часть имени совпадает."""
    seen: dict = {}
    for f in fields:
        base = _base_of(f)
        # Оставляем первое встреченное (уже отсортировано по приоритету)
        if base not in seen:
            seen[base] = f
    return list(seen.values())


# ──────────────────────────────────────────────────────────────
#  Derived
# ──────────────────────────────────────────────────────────────
def suggest_derived(flat: dict) -> dict:
    if not isinstance(flat, dict):
        return {}
    by_cat: dict = {}
    for field in flat.keys():
        cat = classify(field)
        if cat:
            by_cat.setdefault(cat, []).append(field)

    def _best(cat):
        if cat not in by_cat:
            return None
        for f in by_cat[cat]:
            if _is_number(flat.get(f)):
                return f
        return by_cat[cat][0]

    out: dict = {}
    for rule in _derived_rules():
        name = rule.get("name")
        needs = rule.get("needs") or []
        formula = rule.get("formula")
        if not (name and needs and formula):
            continue
        if name in flat:
            continue
        cols = {cat: _best(cat) for cat in needs}
        if any(v is None for v in cols.values()):
            continue
        try:
            out[name] = formula.format(**cols)
        except (KeyError, IndexError):
            continue
    return out


# ──────────────────────────────────────────────────────────────
#  Проверки данных
# ──────────────────────────────────────────────────────────────
def suggest_checks(spec_fields: dict) -> list:
    if not isinstance(spec_fields, dict):
        return []
    field_by_cat: dict = {}
    for spec_key, spec_val in spec_fields.items():
        if not spec_val:
            continue
        if spec_key == "numeric_fields":
            for f in spec_val:
                cat = classify(f)
                if cat and cat not in field_by_cat:
                    field_by_cat[cat] = f
        elif spec_key == "positive_fields":
            for f in spec_val:
                cat = classify(f)
                if cat and cat not in field_by_cat:
                    field_by_cat[cat] = f

    out: list = []
    for rule in _check_rules():
        cat = rule.get("field")
        field = field_by_cat.get(cat)
        if not field:
            continue
        item = {"field": field, "test": rule.get("test"),
                "advice": rule.get("advice", "")}
        if "min" in rule:
            item["min"] = rule["min"]
        if "max" in rule:
            item["max"] = rule["max"]
        if "ref" in rule:
            ref_cat = rule["ref"]
            if ref_cat not in field_by_cat:
                continue
            item["ref"] = field_by_cat[ref_cat]
        out.append(item)
    return out


# ──────────────────────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if not sys.argv[1:]:
        cats = _load()
        print(f"Словарь: {_FILE}")
        print(f"Категорий: {len(cats)}")
        for name, info in sorted(cats.items()):
            roles = ", ".join(info.get("roles") or []) or "—"
            rng = info.get("value_range")
            rng_s = f" range={rng}" if rng else ""
            print(f"  {name:<16} prio={info.get('priority', 0):>3}  "
                  f"roles=[{roles}]{rng_s}")
        print()
        print("Проверьте имя: python -m redcat.data.field_semantics price_rub")
        sys.exit(0)

    for arg in sys.argv[1:]:
        cat, conf, src = classify_full(arg, None)
        roles = ",".join(sorted(roles_of(cat))) if cat else "—"
        print(f"  {arg!r:32} → {cat or '(не распознано)':<14} "
              f"conf={conf:.2f} src={src}  roles=[{roles}]")
