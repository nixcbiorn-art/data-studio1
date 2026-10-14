"""reg_preprocessors — препроцессоры ответов API (регистрируются в PREPROCESSORS)."""
from __future__ import annotations



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
