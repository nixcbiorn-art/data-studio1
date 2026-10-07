"""reg_parsers — парсеры ответов (регистрируются в PARSERS) и parse_payload."""
from __future__ import annotations

import json
import re
from redcat.sources.reg_normalize import dig


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
