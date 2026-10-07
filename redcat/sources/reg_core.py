"""reg_core — SourceSpec и реестр источников (register, all_sources, load_from_dir)."""
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
