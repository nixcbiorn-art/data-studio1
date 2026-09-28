"""
Реестр источников данных
========================
Ключевая идея: новый API добавляется ДЕКЛАРАТИВНО — описанием, а не кодом.
Всё остальное (сбор, нормализация, метрики, аномалии, выгрузка, графики)
работает с любым источником одинаково, потому что опирается только на это
описание.

Два способа добавить источник:

1. Положить JSON-файл в папку `sources/` (Redcat) или `sources_external/`
   (внешние). Ничего программировать не нужно — см. sources/README.
2. Вызвать `register(SourceSpec(...))` из Python, если нужна логика,
   которую в JSON не выразить.

Плейсхолдеры в URL ({region_id}, {country_id} и любые свои из .env с
префиксом REDCAT_) подставляются автоматически.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# ──────────────────────────────────────────────────────────────
#  ОПИСАНИЕ ИСТОЧНИКА
# ──────────────────────────────────────────────────────────────
@dataclass
class SourceSpec:
    """Полное описание одного эндпоинта."""

    key: str                          # техническое имя (= имя таблицы в БД)
    url: str                          # URL с плейсхолдерами {region_id} и т.п.
    title: str = ""                   # человекочитаемое название

    # --- пагинация ---
    page_size: int = 100
    page_size_param: str = "page[size]"
    page_number_param: str = "page[number]"
    next_link_path: tuple = ("links", "next")
    data_path: tuple = ("data",)
    total_path: tuple = ("meta", "total")
    max_records: int | None = None
    split_values_name_field: str = ""
    split_child_name_field: str = ""
    split_values_extra: tuple = ()
    overflow_sort: str | None = None

    # --- обход лимита окна пагинации ---
    split_param: str | None = None
    split_values_from: str | None = None
    split_values_field: str = "id"
    concurrency: int = 8
    split_url_template: str = ""
    preprocess: str = ""

    # --- нормализация ---
    mapping: dict = field(default_factory=dict)
    flatten_depth: int = 3
    derived: dict = field(default_factory=dict)

    # --- семантика полей ---
    id_field: str = "id"
    name_field: str = "name"
    numeric_fields: list = field(default_factory=list)
    group_fields: list = field(default_factory=list)
    track_fields: list = field(default_factory=list)
    positive_fields: list = field(default_factory=list)
    required_fields: list = field(default_factory=list)

    enabled: bool = True
    depends_on: list = field(default_factory=list)

    # --- режим сбора ---
    fetch_mode: str = "http"
    browser_wait_for: str = ""
    browser_wait_ms: int = 0
    browser_headless: bool = True
    
    # --- формат ответа ---
    format: str = "json"              # "json" или "xml" (YRL-фиды)
    xml_record_tag: str = "offer"     # имя тега одной записи в XML
    
    # --- инкрементальный сбор ---
    # Если incremental_param задан — скрапер при наличии сохранённого
    # состояния добавит к URL &<incremental_param>=<last_success-lookback>.
    # Работает только в обычном HTTP-режиме без split_param.
    incremental_param: str = ""
    incremental_lookback_minutes: int = 0
    incremental_format: str = "%Y-%m-%dT%H:%M:%S"

    # --- пул ---
    external: bool = False

    # --- сверка с другой таблицей (обычно — Redcat) ---
    # Формат:
    #   {
    #     "with_table": "apartments",         # имя таблицы-соседа
    #     "on_left": "complex_name",          # колонка этого источника
    #     "on_right": "housing_complex_name", # колонка соседа
    #     "metrics": {"price": "price", "area_total": "total_area"},
    #     "agg": "median",
    #     "filter_right": {"field": "developer_name",
    #                      "op": "contains", "value": "ФСК"},
    #     "normalize_key": true,
    #     "min_group_size": 10,
    #     "thresholds": {"ok": 10, "warn": 20},
    #     "key_aliases": {"скай гарден": "жк скай гарден"}
    #   }
    # Здесь metric-словарь задаёт пары «левая колонка → правая колонка».
    # Все поля необязательны кроме with_table/on_left/on_right/metrics.
    cross_check: dict = field(default_factory=dict)

    def resolved_url(self, params: dict) -> str:
        """Подставляет плейсхолдеры в URL (регистр не важен)."""
        url = self.url
        lookup = {k.lower(): v for k, v in params.items()}

        def substitute(match):
            name = match.group(1).lower()
            if name in lookup:
                return str(lookup[name])
            return match.group(0)

        url = re.sub(r"\{(\w+)\}", substitute, url)
        missing = re.findall(r"\{(\w+)\}", url)
        if missing:
            raise ValueError(
                f"Источник '{self.key}': в URL не подставлены плейсхолдеры "
                f"{missing}. Добавьте их в .env как REDCAT_{missing[0].upper()} "
                f"или в params.")
        return url


_REGISTRY: dict[str, SourceSpec] = {}


def register(spec: SourceSpec) -> SourceSpec:
    if spec.key in _REGISTRY:
        logging.info("Источник '%s' переопределён.", spec.key)
    _REGISTRY[spec.key] = spec
    return spec


def all_sources(only=None) -> list:
    """Источники в порядке зависимостей (топологическая сортировка)."""
    specs = [s for s in _REGISTRY.values() if s.enabled]
    if only:
        wanted = set(only)
        changed = True
        while changed:
            changed = False
            for s in specs:
                if s.key in wanted:
                    for dep in list(s.depends_on) + (
                        [s.split_values_from] if s.split_values_from else []
                    ):
                        if dep and dep not in wanted:
                            wanted.add(dep)
                            changed = True
        specs = [s for s in specs if s.key in wanted]

    ordered, seen = [], set()

    def visit(spec, stack=()):
        if spec.key in seen:
            return
        if spec.key in stack:
            raise ValueError(
                f"Циклическая зависимость источников: {' -> '.join(stack)}")
        deps = list(spec.depends_on)
        if spec.split_values_from:
            deps.append(spec.split_values_from)
        for dep in deps:
            if dep in _REGISTRY:
                visit(_REGISTRY[dep], stack + (spec.key,))
        seen.add(spec.key)
        ordered.append(spec)

    for s in specs:
        visit(s)
    return ordered


def get(key: str):
    return _REGISTRY.get(key)


# ──────────────────────────────────────────────────────────────
#  ЗАГРУЗКА ИСТОЧНИКОВ ИЗ JSON
# ──────────────────────────────────────────────────────────────
def load_from_dir(directory) -> int:
    directory = Path(directory)
    if not directory.exists():
        return 0
    loaded = 0
    for path in sorted(directory.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logging.error("Не удалось прочитать источник %s: %s", path.name, e)
            print(f"  ⚠️ Источник {path.name} пропущен: {e}")
            continue

        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            try:
                spec = _spec_from_dict(item)
            except (TypeError, ValueError) as e:
                logging.error("Некорректное описание источника в %s: %s",
                              path.name, e)
                print(f"  ⚠️ Источник в {path.name} пропущен: {e}")
                continue
            register(spec)
            loaded += 1
    return loaded


def _spec_from_dict(d: dict) -> SourceSpec:
    if not d.get("key") or not d.get("url"):
        raise ValueError("обязательны поля 'key' и 'url'")
    known = SourceSpec.__dataclass_fields__.keys()
    unknown = set(d) - set(known)
    if unknown:
        logging.warning("Источник '%s': неизвестные поля проигнорированы: %s",
                        d.get("key"), ", ".join(sorted(unknown)))
    clean = {k: v for k, v in d.items() if k in known}
    for tuple_field in ("next_link_path", "data_path", "total_path",
                        "split_values_extra"):
        if tuple_field in clean and isinstance(clean[tuple_field], list):
            clean[tuple_field] = tuple(clean[tuple_field])
    return SourceSpec(**clean)


def env_params() -> dict:
    params = {}
    for name, value in os.environ.items():
        if name.startswith("REDCAT_"):
            params[name[len("REDCAT_"):].lower()] = value
    params.setdefault("region_id", os.environ.get("REDCAT_REGION_ID", "20003956"))
    params.setdefault("country_id", os.environ.get("REDCAT_COUNTRY_ID", "2017370"))
    return params


# ──────────────────────────────────────────────────────────────
#  ПРЕПРОЦЕССОРЫ ОТВЕТОВ
# ──────────────────────────────────────────────────────────────
PREPROCESSORS = {}


def register_preprocessor(name):
    def deco(fn):
        PREPROCESSORS[name] = fn
        return fn
    return deco


@register_preprocessor("hc_apartments")
def _flatten_hc_apartments(payload, hc_id=None):
    """Разбирает ответ /housing_complexes/show-apartments-data/{hc_id}."""
    rows = []
    for estate in (payload or {}).get("estates") or []:
        common = {
            "hc_id": hc_id,
            "estate_id": estate.get("estate_id"),
            "dom_number": estate.get("dom_number"),
            "korpus_number": estate.get("korpus_number"),
            "stroenie_number": estate.get("stroenie_number"),
            "total_apartments_estate": estate.get("total_apartments"),
        }
        for apt in estate.get("apartments") or []:
            row = dict(common)
            row.update(apt)
            rows.append(row)
    return rows

@register_preprocessor("fsk_unique_projects")
def _fsk_unique_projects(payload, split_value=None):
    """Из ответа /api/v3/flats достаёт уникальные ЖК (по project.slug).

    /flats отдаёт поток лотов, в каждом — вложенный объект project.
    Нам нужен не поток лотов, а справочник ЖК: одна запись на slug.
    """
    seen = {}
    for item in (payload or {}).get("items") or []:
        proj = item.get("project") or {}
        slug = proj.get("slug")
        if slug and slug not in seen:
            seen[slug] = {
                "_id": proj.get("_id"),
                "slug": slug,
                "title": proj.get("title"),
                "complex_class": proj.get("complexClass"),
                "city": proj.get("city"),
            }
    return list(seen.values())

@register_preprocessor("a101_unique_projects")
def _a101_unique_projects(payload, split_value=None):
    """Из ответа a101 /api/flats/ достаёт уникальные ЖК по project_slug.

    Поле называется `results` (не `items`, как у ФСК), project_slug
    лежит на верхнем уровне каждой записи.
    """
    seen = {}
    items = (payload or {}).get("results") or (payload or {}).get("items") or []
    for item in items:
        slug = item.get("project_slug")
        if slug and slug not in seen:
            seen[slug] = {
                "slug": slug,
                "title": item.get("project") or slug,
                "commercial": item.get("project_commercial") or "",
            }
    return list(seen.values())

# ──────────────────────────────────────────────────────────────
#  НОРМАЛИЗАЦИЯ
# ──────────────────────────────────────────────────────────────
def dig(obj, path):
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


def _stringify_list(items):
    parts = []
    for it in items:
        if isinstance(it, dict):
            label = it.get("name") or it.get("title") or it.get("id")
            extra = it.get("walk_time") or it.get("distance")
            parts.append(f"{label} ({extra})" if label and extra else str(label))
        else:
            parts.append(str(it))
    return "; ".join(p for p in parts if p and p != "None")


def flatten_record(obj, prefix="", depth=0, max_depth=3) -> dict:
    flat = {}
    if not isinstance(obj, dict):
        return {prefix or "value": obj}

    for key, value in obj.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            if depth < max_depth:
                flat.update(flatten_record(value, name, depth + 1, max_depth))
            else:
                flat[name] = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, list):
            flat[name] = _stringify_list(value)
        else:
            flat[name] = value
    return flat


_SAFE_EXPR = re.compile(r"^[\w\s.+\-*/()]+$")


def _eval_derived(expr: str, row: dict):
    if not _SAFE_EXPR.match(expr):
        raise ValueError(f"Недопустимое выражение: {expr!r}")
    scope = {}
    for k, v in row.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            scope[k.replace(".", "_")] = v
    try:
        return eval(expr.replace(".", "_"), {"__builtins__": {}}, scope)  # noqa: S307
    except (NameError, TypeError, ZeroDivisionError, SyntaxError):
        return None


def normalize(records, spec: SourceSpec) -> list:
    rows = []
    for item in records:
        flat = flatten_record(item, max_depth=spec.flatten_depth)

        if spec.mapping:
            row = {}
            for out_col, path in spec.mapping.items():
                if isinstance(path, str) and path in flat:
                    row[out_col] = flat[path]
                else:
                    parts = path.split(".") if isinstance(path, str) else list(path)
                    value = dig(item, parts)
                    row[out_col] = (_stringify_list(value)
                                    if isinstance(value, list) else value)
        else:
            row = flat

        for name, expr in spec.derived.items():
            try:
                row[name] = _eval_derived(expr, row)
            except ValueError as e:
                logging.warning("Источник '%s': %s", spec.key, e)
                row[name] = None

        rows.append(row)
    return rows


# ──────────────────────────────────────────────────────────────
#  МАСТЕР ДОБАВЛЕНИЯ ИСТОЧНИКА (для GUI launcher.py)
# ──────────────────────────────────────────────────────────────
PAGINATION_STYLES = {
    "jsonapi": {"page_size_param": "page[size]", "page_number_param": "page[number]"},
    "simple": {"page_size_param": "per_page", "page_number_param": "page"},
    "offset": {"page_size_param": "limit", "page_number_param": "offset"},
}
PAGINATION_STYLE_LABELS = {
    "jsonapi": "JSON:API — page[size] / page[number] (как у RedCat)",
    "simple": "Простой — per_page / page",
    "offset": "Смещение — limit / offset",
}


def _find_first_list_path(obj, path=()):
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return list(path)
    if isinstance(obj, dict):
        for k, v in obj.items():
            found = _find_first_list_path(v, path + (k,))
            if found is not None:
                return found
    return None


def _find_key_path(obj, predicate, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if predicate(k, v):
                return list(path) + [k]
            found = _find_key_path(v, predicate, path + (k,))
            if found is not None:
                return found
    return None


def suggest_spec(url: str, sample_json=None, key: str = "", title: str = "",
                 pagination_style: str = "jsonapi") -> dict:
    spec = {"key": key or "new_source", "title": title, "url": url}
    spec.update(PAGINATION_STYLES.get(pagination_style,
                                      PAGINATION_STYLES["jsonapi"]))

    if not sample_json:
        return spec

    data_path = _find_first_list_path(sample_json)
    if data_path is None:
        data_path = []
    spec["data_path"] = data_path
    records = dig(sample_json, data_path)
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return spec

    sample_record = records[0]
    flat = flatten_record(sample_record)

    total_path = _find_key_path(
        sample_json,
        lambda k, v: isinstance(v, int) and not isinstance(v, bool)
        and any(w in k.lower() for w in ("total", "count")),
    )
    if total_path:
        spec["total_path"] = total_path

    next_path = _find_key_path(
        sample_json,
        lambda k, v: k.lower() in ("next", "next_page", "next_page_url",
                                   "next_url", "nextpage"),
    )
    if next_path:
        spec["next_link_path"] = next_path

    id_candidates = [k for k in flat if k == "id"] or \
                    [k for k in flat if k.endswith("_id") or k.endswith(".id")]
    spec["id_field"] = id_candidates[0] if id_candidates else "id"

    name_candidates = [k for k in flat
                       if "name" in k.lower() or "title" in k.lower()]
    spec["name_field"] = name_candidates[0] if name_candidates else spec["id_field"]

    numeric_fields = [
        k for k, v in flat.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
        and k != spec["id_field"]
    ]
    spec["numeric_fields"] = numeric_fields[:10]

    return spec


def list_source_files(directory) -> list:
    directory = Path(directory)
    out = []
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            out.append((path, "⚠️ ошибка чтения", "", str(path)))
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            out.append((path, item.get("key", "?"), item.get("title", ""),
                        item.get("url", "")))
    return out


def save_source_file(directory, spec_dict: dict):
    directory = Path(directory)
    directory.mkdir(exist_ok=True)
    key = spec_dict.get("key") or "new_source"
    _spec_from_dict(spec_dict)
    path = directory / f"{key}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(spec_dict, f, ensure_ascii=False, indent=2)
    return path


def delete_source_file(path) -> None:
    Path(path).unlink(missing_ok=True)