"""
Проверка описания источника на живом ответе.
=============================================
Отвечает на вопрос «этот spec вообще сработает?» — до сохранения и до
первого полного сбора.

Вызывается из webapp._probe после построения черновика через
sources.suggest_spec. Принимает уже полученный ответ (bytes) и не
делает сетевых запросов сам.

Что проверяет:
  • стратегии (parse/fetch/pagination) зарегистрированы в реестрах;
  • parse_payload отработал и вернул непустой список;
  • id_field встречается в нормализованных записях и не дублируется;
  • name_field заполнен (хотя бы частично);
  • numeric_fields действительно числа;
  • total_path ведёт к числу.

Возвращает dict с полями:
    parse_ok       — удалось ли применить parse_strategy
    parse_error    — текст ошибки, если parse_ok=False
    items_count    — сколько записей извлёк парсер
    total_reported — общее число, если парсер его нашёл
    columns        — колонки нормализованных записей
    sample         — первые 3 записи после flatten/mapping
    warnings       — [{level, field, message, advice}, ...]
    checks         — [{field, message}, ...]
"""
from __future__ import annotations

from typing import Any

import sources as src


_SPEC_FIELDS = set(src.SourceSpec.__dataclass_fields__)
_TUPLE_FIELDS = ("next_link_path", "data_path", "total_path",
                 "split_values_extra", "cursor_path")


def to_spec(spec_dict) -> tuple:
    """Собирает SourceSpec из черновика. Возвращает (spec, error)."""
    if spec_dict is None:
        return None, "пустое описание"
    if not isinstance(spec_dict, dict):
        return None, (f"описание должно быть объектом, "
                      f"а не {type(spec_dict).__name__}")
    clean = {k: v for k, v in spec_dict.items() if k in _SPEC_FIELDS}
    for tf in _TUPLE_FIELDS:
        if tf in clean and isinstance(clean[tf], list):
            clean[tf] = tuple(clean[tf])
    clean.setdefault("key", "probe")
    clean.setdefault("url", "https://probe.local/")
    try:
        return src.SourceSpec(**clean), None
    except (TypeError, ValueError) as e:
        return None, f"spec не собран: {e}"


def _is_number(v) -> bool:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return True
    if isinstance(v, str):
        try:
            float(v.replace(" ", "").replace("\xa0", "").replace(",", "."))
            return True
        except ValueError:
            return False
    return False


def _looks_numeric(values) -> bool:
    nonempty = [v for v in values if v not in (None, "")]
    return bool(nonempty) and all(_is_number(v) for v in nonempty)


def _normalize_one(item, spec):
    """Повторяет логику sources.normalize для одной записи (без derived)."""
    flat = src.flatten_record(item, max_depth=spec.flatten_depth)
    if not spec.mapping:
        return flat
    row = {}
    for out_col, path in spec.mapping.items():
        if isinstance(path, str) and path in flat:
            row[out_col] = flat[path]
        else:
            parts = path.split(".") if isinstance(path, str) else list(path)
            value = src.dig(item, parts)
            row[out_col] = (src._stringify_list(value)
                            if isinstance(value, list) else value)
    return row


def validate_on_sample(spec_dict, raw, max_items: int = 100) -> dict:
    """Прогоняет spec на одном ответе и возвращает отчёт.

    spec_dict — черновик (dict), обычно из webapp.suggest_spec.
    raw       — bytes исходного ответа (или готовый dict для json_path).
    """
    result: dict[str, Any] = {
        "parse_ok": False, "parse_error": None,
        "items_count": 0, "total_reported": None,
        "columns": [], "sample": [],
        "warnings": [], "checks": [],
    }

    spec, err = to_spec(spec_dict)
    if spec is None:
        result["parse_error"] = err
        result["warnings"].append({
            "level": "critical", "field": "spec", "message": err,
            "advice": "Проверьте черновик описания."})
        return result

    # -- Стратегии зарегистрированы?
    for kind, strategy, registry in (
        ("parse_strategy",      spec.parse_strategy,      src.PARSERS),
        ("fetch_strategy",      spec.fetch_strategy,      src.FETCHERS),
        ("pagination_strategy", spec.pagination_strategy, src.PAGINATORS),
    ):
        if not strategy:
            continue
        if strategy not in registry:
            result["warnings"].append({
                "level": "critical", "field": kind,
                "message": f"неизвестная стратегия «{strategy}»",
                "advice": "Доступные: " + (", ".join(sorted(registry)) or "—"),
            })

    # -- parse_payload
    try:
        parsed = src.parse_payload(raw, spec)
    except Exception as e:  # noqa: BLE001
        result["parse_error"] = f"{type(e).__name__}: {e}"
        result["warnings"].append({
            "level": "critical", "field": "parse_strategy",
            "message": f"парсер не отработал: {e}",
            "advice": "Проверьте data_path и parse_strategy. "
                      "Для HTML-ответа попробуйте next_data / embedded_json / "
                      "json_ld / xml_tag.",
        })
        return result

    items = parsed.get("items") or []
    result["parse_ok"] = True
    result["items_count"] = len(items)
    result["total_reported"] = parsed.get("total")

    if not items:
        result["warnings"].append({
            "level": "critical", "field": "data_path",
            "message": "парсер вернул 0 записей",
            "advice": "Проверьте data_path — возможно, он смотрит не туда. "
                      "Или API действительно вернул пустой список.",
        })
        return result
    result["checks"].append({
        "field": "data_path",
        "message": f"парсер извлёк {len(items)} записей"})

    # -- Нормализация первых записей (без derived — быстро)
    sample_size = min(3, len(items))
    try:
        normalized = [_normalize_one(it, spec) for it in items[:sample_size]
                      if isinstance(it, dict)]
    except Exception as e:  # noqa: BLE001
        result["warnings"].append({
            "level": "warning", "field": "mapping",
            "message": f"нормализация не прошла: {e}",
        })
        normalized = []

    if normalized:
        result["sample"] = normalized
        result["columns"] = list(normalized[0].keys())

    # -- id_field
    if spec.id_field not in (result["columns"] or []):
        result["warnings"].append({
            "level": "warning", "field": "id_field",
            "message": f"«{spec.id_field}» не встречается в первых "
                       f"{len(normalized)} записях",
            "advice": "Проверьте id_field: возможно, колонка называется иначе "
                      "или её перекрыл mapping.",
        })
    else:
        ids = []
        for it in items[:max_items]:
            if not isinstance(it, dict):
                continue
            row = _normalize_one(it, spec)
            ids.append(row.get(spec.id_field))
        filled_ids = [i for i in ids if i is not None]
        unique = len(set(str(i) for i in filled_ids))
        if filled_ids and unique < len(filled_ids):
            result["warnings"].append({
                "level": "critical", "field": "id_field",
                "message": f"«{spec.id_field}» не уникален: "
                           f"{len(filled_ids) - unique} дубликатов "
                           f"в первых {len(filled_ids)} записях",
                "advice": "Неуникальный id затирает записи при сравнении. "
                          "Проверьте, что это действительно первичный ключ.",
            })
        else:
            result["checks"].append({
                "field": "id_field",
                "message": f"уникален в {len(filled_ids)} записях"})

    # -- name_field
    if spec.name_field and spec.name_field in (result["columns"] or []):
        empties = sum(1 for n in normalized
                      if n.get(spec.name_field) in (None, ""))
        if normalized and empties == len(normalized):
            result["warnings"].append({
                "level": "warning", "field": "name_field",
                "message": f"«{spec.name_field}» пуст у всех "
                           f"{len(normalized)} записей",
                "advice": "Название используется в карточках и при сверке "
                          "источников. Стоит указать другое поле.",
            })
        elif normalized:
            result["checks"].append({
                "field": "name_field",
                "message": f"заполнен у {len(normalized) - empties} "
                           f"из {len(normalized)}"})

    # -- numeric_fields
    if spec.numeric_fields and normalized:
        bad = []
        for f in spec.numeric_fields:
            vals = [n.get(f) for n in normalized]
            if not _looks_numeric(vals):
                bad.append(f)
        if bad:
            result["warnings"].append({
                "level": "warning", "field": "numeric_fields",
                "message": f"не похожи на числа: {', '.join(bad)}",
                "advice": "Аномалии, выбросы и средние по этим полям "
                          "работать не будут. Уберите их из numeric_fields "
                          "или проверьте mapping.",
            })
        else:
            result["checks"].append({
                "field": "numeric_fields",
                "message": f"{len(spec.numeric_fields)} полей — числа"})

    # -- total
    if spec.total_path and result["total_reported"] is None:
        result["warnings"].append({
            "level": "warning", "field": "total_path",
            "message": f"total не найден по пути {list(spec.total_path)}",
            "advice": "Без total не проверяется полнота сбора. Найдите "
                      "поле с общим числом в ответе API и укажите его.",
        })
    elif result["total_reported"] is not None:
        result["checks"].append({
            "field": "total_path",
            "message": f"total = {result['total_reported']}"})

    return result
