"""reg_suggest — автоподбор спецификации источника по примеру ответа."""
from __future__ import annotations

from redcat.sources.reg_normalize import dig, flatten_record


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


# ──────────────────────────────────────────────────────────────
#  XML: автодетект тега записи
# ──────────────────────────────────────────────────────────────
# Служебные теги, которые не являются записью
_XML_SKIP_TAGS = frozenset({
    "feed_version", "version", "generator", "title", "description",
    "link", "language", "last_build_date", "pub_date", "copyright",
    "managing_editor", "webmaster", "image", "logo", "docs", "ttl",
    "skipdays", "skiphours", "categories", "category", "channel",
})


def _detect_xml_record_tag(raw) -> str:
    """Определяет имя тега записи в XML-фиде.

    Стратегия:
      1. Парсим XML.
      2. Смотрим прямых детей корня. Если среди них есть повторяющийся
         тег со сложной структурой — это запись.
      3. Если корень — <rss>, заходим в <channel>.
      4. Если <feed>, смотрим детей прямо.

    Примеры:
      <feed><object>...</object><object>...</object></feed>     → object
      <realty-feed><offer>...</offer><offer>...</offer></realty-feed> → offer
      <rss><channel><item>...</item></channel></rss>            → item
    """
    import xml.etree.ElementTree as ET
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        return "offer"

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return "offer"

    def strip_ns(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    def count_children(elem):
        """{tag: count} прямых детей с dict-содержимым."""
        counts = {}
        for child in elem:
            tag = strip_ns(child.tag)
            if tag.lower() in _XML_SKIP_TAGS:
                continue
            if len(child) == 0:
                continue
            counts[tag] = counts.get(tag, 0) + 1
        return counts

    # Сначала — на самом корне
    root_tag = strip_ns(root.tag).lower()
    if root_tag == "rss":
        # <rss><channel><item>...<item>
        for ch in root:
            if strip_ns(ch.tag).lower() == "channel":
                counts = count_children(ch)
                if counts:
                    return max(counts, key=counts.get)
    counts = count_children(root)
    if counts:
        return max(counts, key=counts.get)

    # Корень без повторяющихся детей — ищем глубже
    for level1 in root:
        counts = count_children(level1)
        if counts:
            return max(counts, key=counts.get)

    return "offer"


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
    # Для XML сразу определяем имя тега записи. Иначе xml_tag-парсер
    # ищет <offer> по умолчанию и на profitbase-фиде (<object>)
    # возвращает 0 записей.
    if spec["parse_strategy"] == "xml_tag":
        spec["format"] = "xml"
        spec["xml_record_tag"] = _detect_xml_record_tag(sample_json)
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
        from redcat.data import field_semantics
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
