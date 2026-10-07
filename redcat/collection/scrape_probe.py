"""scrape_probe — пробы живых значений и total источника."""
from __future__ import annotations

import logging
import re
from redcat.sources import registry as src
from redcat.collection.scrape_pages import find_total
from redcat.collection.scrape_split import _get_field, _norm_name


def probe_live_values(url, session, spec, parent=None, limit=3):
    by_name = {}
    if spec.split_child_name_field and spec.split_values_name_field and parent:
        for r in parent:
            n, v = _norm_name(_get_field(r, spec.split_values_name_field)), _get_field(r, spec.split_values_field)
            if n and v is not None:
                by_name.setdefault(n, v)
    field = re.sub(r"^filter\[(.+?)\](\[\])*$", r"\1", spec.split_param or "")
    if not by_name and not field:
        return []
    sep = "&" if "?" in url else "?"
    try:
        raw = src.fetch_raw(
            f"{url}{sep}{spec.page_size_param}={spec.page_size}"
            f"&{spec.page_number_param}=1",
            spec, session=session)
        if raw.get("error") or (raw.get("status") or 0) >= 400:
            return []
        parsed = src.parse_payload(raw.get("body"), spec)
        rows = parsed.get("items") or []
    except ValueError:
        return []
    live = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        if by_name:
            v = by_name.get(_norm_name(_get_field(r, spec.split_child_name_field)))
        else:
            v = _get_field(r, field)
        if v is not None and v not in live:
            live.append(v)
            if len(live) >= limit:
                break
    return live


def probe_total(url, session, spec):
    sep = "&" if "?" in url else "?"
    probe_url = f"{url}{sep}{spec.page_size_param}=1&{spec.page_number_param}=1"
    raw = src.fetch_raw(probe_url, spec, session=session)
    if raw.get("error"):
        logging.warning("[%s] не удалось получить total: %s",
                        spec.key, raw["error"])
        print(f"  ⚠️ [{spec.key}] не удалось получить общий total "
              f"({raw['error']}) — сверка полноты сбора будет недоступна.")
        return None

    status = raw.get("status")
    if status is not None and status >= 400:
        body = (raw.get("body") or b"").decode("utf-8", errors="replace")[:800]
        print(f"  ❌ [{spec.key}] пробный запрос вернул HTTP {status}")
        print(f"     Тело ответа API: {body[:400] or '(пусто)'}")
        logging.error("[%s] пробный запрос: HTTP %d — %s",
                      spec.key, status, body)
        return None

    try:
        parsed = src.parse_payload(raw.get("body"), spec)
        total = parsed.get("total")
        if total is None:
            total = find_total(parsed.get("raw"), spec)
    except ValueError as e:
        logging.warning("[%s] ответ не распарсился: %s", spec.key, e)
        return None

    if total is None:
        print(f"  ⚠️ [{spec.key}] в ответе не нашлось общего числа записей — "
              f"сверить полноту сбора будет не с чем.")
    return total
