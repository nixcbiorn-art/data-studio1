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

    # --- заголовки запроса ---
    # Пробрасываются в fetcher как есть. Нужны для API, которые
    # блокируют дефолтный User-Agent requests (MR Group, servicepipe).
    # Пример: {"User-Agent": "Mozilla/5.0 ...", "Referer": "..."}
    headers: dict = field(default_factory=dict)

    # --- формат ответа ---
    format: str = "json"              # "json" или "xml" (YRL-фиды)
    xml_record_tag: str = "offer"     # имя тега одной записи в XML

    # --- стратегия парсинга ответа ---
    # Пусто = авто: "json_path" для format="json", "xml_tag" для format="xml".
    # Значение — имя зарегистрированного парсера в реестре PARSERS.
    # Реестр расширяется через register_parser(name).
    parse_strategy: str = ""
    # Подсказка для парсеров, которые ищут что-то в HTML:
    #   embedded_json / next_data — id или class <script>-тега
    #   (для next_data по умолчанию используется "__NEXT_DATA__")
    parse_hint: str = ""

    # --- стратегия скачивания одной страницы ---
    # Пусто = авто по fetch_mode: "browser" → browser, иначе http.
    # Реестры FETCHERS / ASYNC_FETCHERS живут в этом файле, регистрация
    # конкретных стратегий — в redcat_scraper.py (там есть requests и aiohttp).
    fetch_strategy: str = ""

    # --- стратегия пагинации ---
    # Пусто = авто: next_link если next_link_path непустой, иначе
    # page_number или offset (зависит от имени page_number_param).
    # Значение — имя зарегистрированного пагинатора в реестре PAGINATORS.
    pagination_strategy: str = ""
    # Для стратегии cursor — путь в ответе до следующего курсора.
    # Например: ["meta","pagination","next_cursor"]
    cursor_path: tuple = ()

    # --- браузерные стратегии ---
    # Для fetch_strategy="browser_xhr": подстрока URL, по которой ловим
    # XHR/fetch-ответы (например "/api/v3/flats"). Если пусто — берётся
    # первый JSON-ответ.
    browser_xhr_pattern: str = ""

    # --- защита от бана ---
    # 0 = не ограничивать (быстро, но можно словить 429 на больших объёмах).
    # >0 = N запросов в секунду на хост. Глобальный дефолт — переменная
    # окружения REDCAT_RATE_LIMIT_RPS.
    rate_limit_rps: float = 0.0
    # Сколько подряд ошибок от источника (сеть, 5xx, 429) — до временного
    # отключения. Дефолт — REDCAT_CIRCUIT_FAILURES или 8.
    circuit_breaker_failures: int = 0
    # На сколько секунд отключить источник при срабатывании breaker.
    # Дефолт — REDCAT_CIRCUIT_COOLDOWN или 300.
    circuit_breaker_cooldown_sec: int = 0
    
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
#  ПРЕПРОЦЕССОР GK OSNOVA
# ──────────────────────────────────────────────────────────────
# У квартир в ответе filter только project_id, без имени ЖК.
# Preprocessor проставляет project_id (из split value) и, лениво,
# project_name — тянет из /api/building-objects/projects, кэширует.

@register_preprocessor("osnova_flats")
def _osnova_flats(payload, project_id=None):
    """Разворачивает data.flats[] и обогащает именем ЖК."""
    # Ленивый кэш {id: name}, один раз за процесс
    if _osnova_flats._cache is None:
        try:
            import requests as _rq
            r = _rq.get(
                "https://gk-osnova.ru/api/building-objects/projects",
                headers={
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; "
                                   "x64) AppleWebKit/537.36 (KHTML, like "
                                   "Gecko) Chrome/122.0.0.0 Safari/537.36"),
                    "Accept": "application/json",
                    "Referer": "https://gk-osnova.ru/",
                },
                timeout=20)
            items = r.json().get("data") or []
            _osnova_flats._cache = {str(it.get("id")): it.get("name")
                                     for it in items if it.get("id") is not None}
        except Exception as _e:
            import logging as _log
            _log.warning("osnova_flats: не удалось загрузить справочник: %s", _e)
            _osnova_flats._cache = {}
    cache = _osnova_flats._cache

    pid = str(project_id) if project_id is not None else None
    pname = cache.get(pid)

    rows = []
    skipped_non_res = 0
    for f in ((payload or {}).get("data", {}) or {}).get("flats") or []:
        layout = f.get("layout") or {}
        # Пропускаем нежилые помещения (коммерция, кладовки и т.п.):
        # они не сопоставимы с Квартира/Апартамент/Таунхаус в Redcat
        # и только портят медиану площади.
        if layout.get("type") == "non-residential":
            skipped_non_res += 1
            continue
        row = dict(f)
        row["project_id"] = pid
        row["project_name"] = pname
        rows.append(row)
    if skipped_non_res:
        import logging as _log
        _log.info("osnova_flats: пропущено %d нежилых помещений", skipped_non_res)
    return rows

_osnova_flats._cache = None


# ──────────────────────────────────────────────────────────────
#  ПРЕПРОЦЕССОР MR GROUP
# ──────────────────────────────────────────────────────────────
# У MR в поле price — базовая цена без скидки. Redcat хранит
# цену СО СКИДКОЙ. Чтобы сверка сходилась, перезаписываем price
# на discount.price (если он заполнен) и meter_price — на
# discount.meter_price. Если скидки нет — оставляем базовую.

@register_preprocessor("mr_flats")
def _mr_flats(payload, split_value=None):
    """item.price → item.discount.price, если скидка есть."""
    rows = []
    for item in (payload or {}).get("items") or []:
        row = dict(item)
        d = item.get("discount") or {}
        if d.get("price"):
            row["price"] = d["price"]
            row["price_base"] = item.get("price")
        if d.get("meter_price"):
            row["meter_price"] = d["meter_price"]
            row["meter_price_base"] = item.get("meter_price")
        rows.append(row)
    return rows


# ──────────────────────────────────────────────────────────────
#  ПАРСЕРЫ ОТВЕТА
# ──────────────────────────────────────────────────────────────
# Стратегия разбора ответа: превращает сырой HTTP-ответ в единый словарь
#   {"items": [...], "total": int | None, "raw": <что-то>}
# Ключ парсера хранится в SourceSpec.parse_strategy. Пусто → авто по format.
#
# Регистрация своей стратегии — так же, как для PREPROCESSORS:
#     @register_parser("my_format")
#     def _parse_my_format(raw, spec): ...
PARSERS: dict = {}


def register_parser(name: str):
    def deco(fn):
        PARSERS[name] = fn
        return fn
    return deco


def _xml_elem_to_dict(elem):
    """XML-элемент → dict. Атрибуты и текст — на верхний уровень,
    повторяющиеся дочерние теги становятся списками, namespace срезается.
    """
    result = dict(elem.attrib)
    children = list(elem)
    if not children:
        text = (elem.text or "").strip()
        if result:
            if text:
                result["#text"] = text
            return result
        return text or None
    grouped = {}
    for child in children:
        tag = child.tag.split("}")[-1]
        val = _xml_elem_to_dict(child)
        if tag in grouped:
            if not isinstance(grouped[tag], list):
                grouped[tag] = [grouped[tag]]
            grouped[tag].append(val)
        else:
            grouped[tag] = val
    result.update(grouped)
    return result


@register_parser("json_path")
def _parse_json_path(raw, spec):
    """По умолчанию для JSON: массив записей берётся по spec.data_path."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, TypeError) as e:
            raise ValueError(f"ответ не разобрался как JSON: {e}") from e
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            raise ValueError(f"ответ не разобрался как JSON: {e}") from e
    else:
        data = raw

    items = dig(data, spec.data_path)
    if not isinstance(items, list):
        items = []
    return {
        "items": items,
        "total": dig(data, spec.total_path),
        "raw": data,
    }


@register_parser("xml_tag")
def _parse_xml_tag(raw, spec):
    """YRL-подобные фиды: одна запись = один <offer>/<item>/<record>."""
    import xml.etree.ElementTree as ET
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("xml_tag ожидает bytes или str, а не "
                         f"{type(raw).__name__}")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ValueError(f"XML не разобрался: {e}") from e

    tag = spec.xml_record_tag or "item"
    items = [_xml_elem_to_dict(el) for el in root.iter()
             if el.tag.split("}")[-1] == tag]
    return {"items": items, "total": len(items), "raw": root}


@register_parser("next_data")
def _parse_next_data(raw, spec):
    """JSON внутри <script id="__NEXT_DATA__"> (Next.js)."""
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8", errors="replace")
    elif isinstance(raw, str):
        text = raw
    else:
        items = dig(raw, spec.data_path)
        return {"items": items if isinstance(items, list) else [],
                "total": dig(raw, spec.total_path), "raw": raw}

    marker = spec.parse_hint or "__NEXT_DATA__"
    pattern = (r'<script[^>]+id=["\']' + re.escape(marker)
               + r'["\'][^>]*>(.+?)</script>')
    m = re.search(pattern, text, re.S)
    if not m:
        raise ValueError(
            f"<script id='{marker}'> не найден в HTML. Проверьте parse_hint.")
    try:
        data = json.loads(m.group(1))
    except ValueError as e:
        raise ValueError(f"JSON внутри <script id='{marker}'> не разобрался: {e}") from e

    items = dig(data, spec.data_path)
    if not isinstance(items, list):
        items = []
    return {"items": items, "total": dig(data, spec.total_path), "raw": data}


@register_parser("embedded_json")
def _parse_embedded_json(raw, spec):
    """Любой <script type="application/json">...</script> в HTML."""
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8", errors="replace")
    elif isinstance(raw, str):
        text = raw
    else:
        raise ValueError("embedded_json ожидает HTML, а не "
                         f"{type(raw).__name__}")

    if spec.parse_hint:
        pattern = (r'<script[^>]+(?:id|class)=["\'][^"\']*'
                   + re.escape(spec.parse_hint)
                   + r'[^"\']*["\'][^>]*>(.+?)</script>')
    else:
        pattern = r'<script[^>]+type=["\']application/json["\'][^>]*>(.+?)</script>'

    m = re.search(pattern, text, re.S)
    if not m:
        hint = f" с подсказкой «{spec.parse_hint}»" if spec.parse_hint else ""
        raise ValueError(f"JSON-скрипт{hint} не найден в HTML")
    try:
        data = json.loads(m.group(1))
    except ValueError as e:
        raise ValueError(f"JSON внутри script не разобрался: {e}") from e

    if isinstance(data, list):
        return {"items": data, "total": len(data), "raw": data}
    items = dig(data, spec.data_path)
    if not isinstance(items, list):
        items = []
    return {"items": items, "total": dig(data, spec.total_path), "raw": data}


@register_parser("json_ld")
def _parse_json_ld(raw, spec):
    """Микроразметка <script type="application/ld+json"> (Schema.org)."""
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8", errors="replace")
    elif isinstance(raw, str):
        text = raw
    else:
        raise ValueError("json_ld ожидает HTML, а не "
                         f"{type(raw).__name__}")

    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.+?)</script>',
        text, re.S)
    items = []
    for b in blocks:
        try:
            data = json.loads(b)
        except ValueError:
            continue
        if isinstance(data, list):
            items.extend(data)
        elif isinstance(data, dict):
            graph = data.get("@graph")
            if isinstance(graph, list):
                items.extend(graph)
            else:
                items.append(data)
    return {"items": items, "total": len(items), "raw": items}


# ──────────────────────────────────────────────────────────────
#  FETCH-СТРАТЕГИИ (скачивание ОДНОЙ страницы)
# ──────────────────────────────────────────────────────────────
# Фетчер принимает (url, spec, **kwargs) и возвращает единый словарь:
#     {"status": int | None, "body": bytes | None,
#      "headers": dict, "error": str | None}
# Ничего не парсит и не решает, повторять ли запрос — это политика
# вызывающего кода. Регистрируется из redcat_scraper.py, где есть
# requests и aiohttp.
#
# Имя стратегии берётся из spec.fetch_strategy. Если пусто — авто:
#   fetch_mode == "browser" → "browser", иначе "http".
FETCHERS: dict = {}
ASYNC_FETCHERS: dict = {}


def register_fetcher(name: str):
    def deco(fn):
        FETCHERS[name] = fn
        return fn
    return deco


def register_async_fetcher(name: str):
    def deco(fn):
        ASYNC_FETCHERS[name] = fn
        return fn
    return deco


def _pick_fetch_strategy(spec) -> str:
    if spec is not None and getattr(spec, "fetch_strategy", ""):
        return spec.fetch_strategy
    if spec is not None and getattr(spec, "fetch_mode", "http") == "browser":
        return "browser"
    return "http"


def fetch_raw(url, spec, **kwargs) -> dict:
    """Синхронное скачивание одной страницы через стратегию spec.

    kwargs передаются в фетчер как есть. Для http обычно нужен
    session=<requests.Session>. Для browser — обычно ничего.
    """
    name = _pick_fetch_strategy(spec)
    fetcher = FETCHERS.get(name)
    if fetcher is None:
        raise ValueError(
            f"Источник «{getattr(spec, 'key', '?')}»: неизвестная стратегия "
            f"скачивания «{name}». Доступные: "
            f"{', '.join(sorted(FETCHERS)) or '(нет зарегистрированных)'}.")
    return fetcher(url, spec, **kwargs)


async def async_fetch_raw(url, spec, **kwargs) -> dict:
    """Асинхронное скачивание одной страницы через стратегию spec.

    Если spec=None — используется "http" (совместимость со старым кодом
    preflight, где spec не всегда передавался).
    """
    name = _pick_fetch_strategy(spec) if spec is not None else "http"
    fetcher = ASYNC_FETCHERS.get(name)
    if fetcher is None:
        raise ValueError(
            f"Источник «{getattr(spec, 'key', '?')}»: неизвестная асинхронная "
            f"стратегия скачивания «{name}». Доступные: "
            f"{', '.join(sorted(ASYNC_FETCHERS)) or '(нет зарегистрированных)'}.")
    return await fetcher(url, spec, **kwargs)


# ──────────────────────────────────────────────────────────────
#  ПАГИНАЦИИ
# ──────────────────────────────────────────────────────────────
# Пагинатор принимает:
#   data          — raw payload ответа (то, что вернул парсер в "raw")
#   spec          — SourceSpec
#   current_url   — URL только что полученной страницы
#   page_number   — её номер
#   got_rows      — сколько записей было на этой странице
#   collected     — сколько собрано всего
#   total         — сколько заявлено API (или None)
# Возвращает URL следующей страницы или None, если обход завершён.
#
# Регистрация:
#     @register_paginator("my_mode")
#     def _paginate_my_mode(...): ...
PAGINATORS: dict = {}


def register_paginator(name: str):
    def deco(fn):
        PAGINATORS[name] = fn
        return fn
    return deco


def set_query_param(url: str, name: str, value) -> str:
    """Заменяет или добавляет query-параметр в URL (с сохранением остальных)."""
    import urllib.parse as _up
    parts = _up.urlsplit(url)
    pairs = _up.parse_qsl(parts.query, keep_blank_values=True)
    pairs = [(k, v) for k, v in pairs if k != name]
    pairs.append((name, str(value)))
    return _up.urlunsplit(parts._replace(query=_up.urlencode(pairs)))


def _paginated_url(current_url, spec, page_number, collected):
    """Стандартная «следующая страница»: page_size + (номер или offset)."""
    is_offset = "offset" in (spec.page_number_param or "").lower()
    value = collected if is_offset else page_number + 1
    url = set_query_param(current_url, spec.page_size_param, spec.page_size)
    url = set_query_param(url, spec.page_number_param, value)
    return url


@register_paginator("next_link")
def _paginate_next_link(data, spec, current_url, page_number, got_rows,
                        collected, total):
    """Идём ровно по ссылке из ответа; если её нет — обход завершён."""
    import urllib.parse as _up
    nxt = dig(data, spec.next_link_path)
    if not isinstance(nxt, str) or not nxt:
        return None
    absolute = _up.urljoin(current_url, nxt)
    return set_query_param(absolute, spec.page_size_param, spec.page_size)


@register_paginator("page_number")
def _paginate_page_number(data, spec, current_url, page_number, got_rows,
                          collected, total):
    if total is not None and collected >= total:
        return None
    if total is None and got_rows < spec.page_size:
        return None
    return _paginated_url(current_url, spec, page_number, collected)


@register_paginator("offset")
def _paginate_offset(data, spec, current_url, page_number, got_rows,
                     collected, total):
    if total is not None and collected >= total:
        return None
    if got_rows == 0:
        return None
    return _paginated_url(current_url, spec, page_number, collected)


@register_paginator("cursor")
def _paginate_cursor(data, spec, current_url, page_number, got_rows,
                     collected, total):
    """Курсорная пагинация: следующий курсор лежит по spec.cursor_path."""
    if not spec.cursor_path:
        return None
    cursor = dig(data, spec.cursor_path)
    if cursor is None or cursor == "":
        return None
    return set_query_param(current_url, "cursor", cursor)


@register_paginator("stop")
def _paginate_stop(data, spec, current_url, page_number, got_rows,
                   collected, total):
    return None


@register_paginator("auto")
def _paginate_auto(data, spec, current_url, page_number, got_rows,
                   collected, total):
    """Приоритет next_link (если он есть в ответе), иначе номер/offset."""
    import urllib.parse as _up
    nxt = dig(data, spec.next_link_path) if spec.next_link_path else None
    if isinstance(nxt, str) and nxt:
        absolute = _up.urljoin(current_url, nxt)
        return set_query_param(absolute, spec.page_size_param, spec.page_size)
    if total is not None and collected >= total:
        return None
    if total is None and got_rows < spec.page_size:
        return None
    return _paginated_url(current_url, spec, page_number, collected)


def _pick_pagination_strategy(spec) -> str:
    if getattr(spec, "pagination_strategy", ""):
        return spec.pagination_strategy
    if "offset" in (spec.page_number_param or "").lower():
        return "offset"
    return "auto"


def compute_next_url(data, spec, current_url, page_number, got_rows,
                     collected, total):
    """Диспетчер: возвращает URL следующей страницы или None."""
    name = _pick_pagination_strategy(spec)
    pager = PAGINATORS.get(name)
    if pager is None:
        raise ValueError(
            f"Источник «{spec.key}»: неизвестная стратегия пагинации "
            f"«{name}». Доступные: {', '.join(sorted(PAGINATORS))}.")
    return pager(data, spec, current_url, page_number, got_rows,
                 collected, total)


# ──────────────────────────────────────────────────────────────
#  БРАУЗЕРНЫЕ FETCH-СТРАТЕГИИ
# ──────────────────────────────────────────────────────────────
# Регистрируются здесь, а не в scraper'е: они не требуют requests/aiohttp,
# только browser_fetch. Импорт browser_fetch — ленивый, внутри функций,
# чтобы sources.py оставался лёгким для импорта без Playwright.
#
# Контракт тот же, что у http-фетчеров:
#     {"status": int|None, "body": bytes|None, "headers": dict, "error": str|None}
# Для "browser" body — это HTML страницы.
# Для "browser_xhr" body — это JSON-текст перехваченного XHR-ответа,
# который дальше разберёт обычный parse_strategy="json_path".


def _import_browser_fetch():
    try:
        import browser_fetch
        return browser_fetch
    except ImportError as e:
        raise RuntimeError(
            "Для browser-фетчеров нужен Playwright. Установите:\n"
            "    pip install playwright\n"
            "    playwright install chromium"
        ) from e


@register_fetcher("browser")
def _fetch_browser_page(url, spec, fetcher=None, **_kw):
    """Скачивает HTML страницы через Playwright.

    Если передан `fetcher=` (открытый BrowserFetcher) — использует его;
    иначе открывает свой и закрывает после вызова. Для одиночных вызовов
    (probe) это норма; для серийных — передавайте fetcher явно.
    """
    bf = _import_browser_fetch()
    if not hasattr(bf, "BrowserFetcher"):
        return {"status": None, "body": None, "headers": {},
                "error": "browser_fetch.py не найден или Playwright не установлен."}

    own = fetcher is None
    if own:
        fetcher = bf.BrowserFetcher(
            getattr(spec, "key", "probe"),
            headless=getattr(spec, "browser_headless", True))
        fetcher.__enter__()
    try:
        html = fetcher.fetch(
            url,
            wait_for=getattr(spec, "browser_wait_for", "") or "",
            wait_ms=int(getattr(spec, "browser_wait_ms", 0) or 0),
        )
        body = (html or "").encode("utf-8")
        return {"status": 200, "body": body, "headers": {}, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "body": None, "headers": {},
                "error": f"{type(e).__name__}: {e}"}
    finally:
        if own:
            try:
                fetcher.__exit__(None, None, None)
            except Exception:
                pass


@register_fetcher("browser_xhr")
def _fetch_browser_xhr(url, spec, fetcher=None, **_kw):
    """Открывает страницу и возвращает JSON одного из её XHR/fetch-ответов.

    По умолчанию (spec.browser_xhr_pattern пуст) берётся первый JSON-ответ.
    Если задан паттерн — берётся первый, чей URL содержит подстроку.

    body возвращается как JSON-текст — далее parse_strategy="json_path"
    разбирает его как обычный ответ API.
    """
    import json as _json
    bf = _import_browser_fetch()
    if not hasattr(bf, "BrowserFetcher"):
        return {"status": None, "body": None, "headers": {},
                "error": "browser_fetch.py не найден или Playwright не установлен."}

    own = fetcher is None
    if own:
        fetcher = bf.BrowserFetcher(
            getattr(spec, "key", "probe"),
            headless=getattr(spec, "browser_headless", True))
        fetcher.__enter__()
    try:
        pattern = getattr(spec, "browser_xhr_pattern", "") or ""
        captured = fetcher.capture_json_responses(
            url, pattern,
            wait_for=getattr(spec, "browser_wait_for", "") or "",
            wait_ms=int(getattr(spec, "browser_wait_ms", 0) or 0),
        )
        if not captured:
            return {"status": None, "body": None, "headers": {},
                    "error": f"ни одного JSON-XHR по паттерну «{pattern}» "
                             f"не поймано"}
        payload = captured[0]
        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return {"status": 200, "body": body, "headers": {}, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "body": None, "headers": {},
                "error": f"{type(e).__name__}: {e}"}
    finally:
        if own:
            try:
                fetcher.__exit__(None, None, None)
            except Exception:
                pass


def parse_payload(raw, spec) -> dict:
    """Диспетчер: превращает сырой ответ в {'items', 'total', 'raw'}.

    Стратегия берётся из spec.parse_strategy. Если пусто — по spec.format:
    "xml" → xml_tag, иначе json_path. Неизвестное имя стратегии — понятное
    исключение с перечислением доступных.
    """
    strategy = spec.parse_strategy
    if not strategy:
        strategy = "xml_tag" if getattr(spec, "format", "json") == "xml" else "json_path"
    parser = PARSERS.get(strategy)
    if parser is None:
        raise ValueError(
            f"Источник «{spec.key}»: неизвестная стратегия парсинга "
            f"«{strategy}». Доступные: {', '.join(sorted(PARSERS))}.")
    return parser(raw, spec)





# ──────────────────────────────────────────────────────────────
#  ПРЕПРОЦЕССОРЫ ДЛЯ АРХИТЕКТУРЫ /apartments/fast
# ──────────────────────────────────────────────────────────────
# UI Redcat показывает цену СО СКИДКОЙ. /apartments/fast возвращает
# именно её в поле price. Но фильтруется он по estate_id (корпус),
# а не по housing_complex_id. Список корпусов лежит в ответе
# housing_complexes/show-apartments-data/{hc_id}.
#
# Эти два препроцессора связывают три уровня:
#   housing_complexes → apartments_estates → apartments.

@register_preprocessor("extract_estates")
def _extract_estates(payload, hc_id=None):
    """show-apartments-data/{hc_id} → список корпусов ЖК."""
    rows = []
    for est in (payload or {}).get("estates") or []:
        rows.append({
            "estate_id": est.get("estate_id"),
            "housing_complex_id": hc_id,
            "dom_number": est.get("dom_number"),
            "korpus_number": est.get("korpus_number"),
            "stroenie_number": est.get("stroenie_number"),
            "address": est.get("address"),
            "total_apartments": est.get("total_apartments"),
            "floor_min": est.get("floor_number_min"),
            "floor_max": est.get("floor_number_max"),
            "delivery_date": est.get("delivery_date"),
            "delivery_date_formatted": est.get("delivery_date_formatted"),
        })
    return rows


@register_preprocessor("fast_apartments")
def _fast_apartments(payload, estate_id=None):
    """apartments/fast → список лотов + поле estate_id."""
    rows = []
    for it in (payload or {}).get("data") or []:
        row = dict(it)
        row["estate_id"] = estate_id
        rows.append(row)
    return rows

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
    if "*" in expr and "**" in expr.replace(" ", ""):
        raise ValueError(
            f"Оператор ** запрещён — вычисление слишком дорогое: {expr!r}")
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


def _detect_parse_strategy(sample) -> str:
    """Определяет стратегию парсинга по образцу ответа."""
    if sample is None:
        return "json_path"
    if isinstance(sample, (dict, list)):
        return "json_path"
    if isinstance(sample, (bytes, bytearray)):
        text = sample.decode("utf-8", errors="replace")
    elif isinstance(sample, str):
        text = sample
    else:
        return "json_path"

    head = text.lstrip()[:200]
    if not head.startswith("<"):
        return "json_path"
    if "__NEXT_DATA__" in text:
        return "next_data"
    if "application/ld+json" in text:
        return "json_ld"
    if 'type="application/json"' in text:
        return "embedded_json"
    return "xml_tag"


def suggest_spec(url: str, sample_json=None, key: str = "", title: str = "",
                 pagination_style: str = "jsonapi") -> dict:
    spec = {"key": key or "new_source", "title": title, "url": url}
    spec.update(PAGINATION_STYLES.get(pagination_style,
                                      PAGINATION_STYLES["jsonapi"]))

    if not sample_json:
        return spec

    spec["parse_strategy"] = _detect_parse_strategy(sample_json)
    data_path = _find_first_list_path(sample_json)
    if data_path is None:
        data_path = []
    spec["data_path"] = data_path
    records = dig(sample_json, data_path)
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return spec

    # Разворачиваем несколько записей — для голосования по типам значений.
    # Больше 50 не нужно: если поле пустое в 50 записях, оно и в 5000 будет пустым.
    sample_records = [r for r in records[:50] if isinstance(r, dict)]
    flats = [flatten_record(r) for r in sample_records] or [{}]
    flat = flats[0]

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

    # Угадывание полей spec — по семантическому словарю.
    # field_semantics.classify() знает, что price / cost / цена — это
    # одна и та же категория, а housing_complex_id — это foreign_id,
    # а не «настоящий» id записи. Если словарь отсутствует или сломан —
    # работает минимальный фолбэк.
    try:
        import field_semantics
        guessed = field_semantics.suggest_spec_fields(flats)
    except Exception:
        guessed = {}
        # Минимальный резерв, чтобы spec был рабочим даже без словаря.
        id_candidates = [k for k in flat if k == "id"] or \
                        [k for k in flat if k.endswith("_id") or k.endswith(".id")]
        guessed["id_field"] = id_candidates[0] if id_candidates else "id"
        name_candidates = [k for k in flat
                           if "name" in k.lower() or "title" in k.lower()]
        guessed["name_field"] = name_candidates[0] if name_candidates \
                                else guessed["id_field"]
        guessed["numeric_fields"] = [
            k for k, v in flat.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and k != guessed["id_field"]
        ][:10]

    for key, value in guessed.items():
        if key == "date_fields":
            continue  # в SourceSpec такого поля нет, оно только для UI
        spec[key] = value

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