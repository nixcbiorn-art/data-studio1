"""scrape_pages — постраничный сбор: пагинация, определение total, JSON-запросы."""
from __future__ import annotations

import asyncio
import logging
import time
from redcat.sources import registry as src
from redcat.collection.scrape_config import MAX_RETRIES, RETRY_DELAY_SEC
from redcat.collection.scrape_guard import _rps_for


# ──────────────────────────────────────────────────────────────
#  СБОР ДАННЫХ
# ──────────────────────────────────────────────────────────────
TOTAL_PATHS = (
    ("meta", "total"),
    ("meta", "total_count"),
    ("meta", "count"),
    ("meta", "pagination", "total"),
    ("meta", "pagination", "total_count"),
    ("meta", "page", "total"),
    ("pagination", "total"),
    ("total",),
    ("total_count",),
    ("count",),
)


def find_total(data, spec):
    value = src.dig(data, spec.total_path)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    for path in TOTAL_PATHS:
        value = src.dig(data, path)
        if isinstance(value, int) and not isinstance(value, bool):
            logging.info("[%s] total найден по пути %s (в описании указан %s).",
                         spec.key, path, spec.total_path)
            return value
    return None


# Реализация перенесена в sources.py — доступна как src.set_query_param.
# Алиас сохранён, чтобы не трогать существующие вызовы по всему файлу.
_set_query_param = src.set_query_param


def _next_url(data, spec, current_url, page_number, got_rows, collected, total):
    """Определяет адрес следующей страницы.

    Делегирует в src.compute_next_url — там реестр стратегий пагинации
    (auto / next_link / page_number / offset / cursor / stop). data — это
    raw payload ответа (обычно то, что вернул парсер в поле "raw").
    """
    return src.compute_next_url(data, spec, current_url, page_number,
                                got_rows, collected, total)


def _window_limit_hint(body) -> bool:
    text = (body or "").lower()
    return any(marker in text for marker in (
        "result window", "from + size", "max_result_window", "too_many_buckets",
        "window is too large"))


def _looks_deterministic(body: str) -> bool:
    low = (body or "").lower()
    markers = (
        "sqlstate", "syntax error", "illuminate\\database", "unknown column",
        "undefined column", "no such column", "traceback (most recent call last)",
        "fatal error", "pdoexception", "errorexception", "typeerror:",
        "undefined method", "call to a member function", "division by zero",
        "argumentcounterror", "column not found", "invalid column",
    )
    return any(m in low for m in markers)


def fetch_pages(base_url, session, spec, label="", max_pages=100000,
                treat_as_complete=True):
    """Синхронный обход пагинации. Возвращает (записи, incomplete, total)."""
    url = _set_query_param(
        _set_query_param(base_url, spec.page_size_param, spec.page_size),
        spec.page_number_param,
        0 if "offset" in (spec.page_number_param or "").lower() else 1)
    all_data, page, incomplete, reported_total = [], 1, False, None
    seen_ids, stalled, visited, capped = set(), False, set(), False

    print(f"  📡 [{label}] Загрузка...", end=" ", flush=True)
    while url and page <= max_pages:
        if url in visited:
            logging.warning("[%s] повтор адреса страницы — обход остановлен: %s",
                            label, url)
            break
        visited.add(url)

        data = None
        for attempt in range(1, MAX_RETRIES + 1):
            raw = src.fetch_raw(url, spec, session=session)
            if raw.get("error"):
                if raw.get("error_kind") == "circuit_open":
                    print(f"\n  ⛔ [{label}] {raw['error']}. "
                          f"Источник пропускается, повторите запуск позже.")
                    incomplete = True
                    data = None
                    break
                logging.warning("[%s] стр.%d попытка %d/%d: %s",
                                label, page, attempt, MAX_RETRIES, raw["error"])
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SEC * attempt)
                else:
                    incomplete = True
                continue

            status = raw.get("status")
            if status is not None and status >= 400:
                body = (raw.get("body") or b"").decode("utf-8", errors="replace")[:800]
                if _window_limit_hint(body):
                    print(f"\n  ⛔ [{label}] API упёрся в лимит окна пагинации "
                          f"на странице {page} (собрано {len(all_data)}).")
                    logging.error("[%s] лимит окна пагинации: %s", label, body)
                    incomplete = True
                    data = None
                    break

                print(f"\n  ❌ [{label}] стр.{page}: HTTP {status}")
                print(f"     Тело ответа API: {body[:400] or '(пусто)'}")
                logging.error("[%s] стр.%d: HTTP %d — %s",
                             label, page, status, body)

                if _looks_deterministic(body):
                    print(f"  ⛔ Похоже на поломку самого запроса.")
                    incomplete = True
                    data = None
                    break

                if attempt < MAX_RETRIES:
                    print(f"     Повтор {attempt}/{MAX_RETRIES}...")
                    time.sleep(RETRY_DELAY_SEC * attempt)
                    continue
                incomplete = True
                data = None
                break

            try:
                data = src.parse_payload(raw.get("body"), spec)
            except ValueError as e:
                logging.error("[%s] стр.%d: не удалось разобрать ответ: %s",
                              label, page, e)
                print(f"\n  ❌ [{label}] стр.{page}: ответ не разобран: {e}")
                incomplete = True
                data = None
                break
            break
        if data is None:
            break

        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            logging.warning("[%s] стр.%d: в ответе нет списка записей.",
                            label, page)
            break
        if page > 1 and not items:
            break

        page_ids = {str(it.get(spec.id_field)) for it in items
                    if isinstance(it, dict) and it.get(spec.id_field) is not None}
        if page_ids and page > 1 and page_ids <= seen_ids:
            print(f"\n  ⚠️ [{label}] страница {page} повторяет предыдущую — "
                  f"обход остановлен, собрано {len(all_data)}.")
            logging.warning("[%s] пагинация не двигается на странице %d", label, page)
            stalled = incomplete = True
            break
        seen_ids |= page_ids

        all_data.extend(items)
        if page % 10 == 0 or page == 1:
            print(f"[{page}:{len(all_data)}]", end=" ", flush=True)

        if reported_total is None:
            reported_total = data.get("total") if isinstance(data, dict) else None
        if reported_total is None:
            reported_total = find_total(data.get("raw") if isinstance(data, dict) else data,
                                        spec)

        if spec.max_records and len(all_data) >= spec.max_records:
            del all_data[spec.max_records:]
            capped = True
            break

        url = _next_url(data.get("raw") if isinstance(data, dict) else data,
                        spec, url, page, len(items),
                        len(all_data), reported_total)
        if url:
            page += 1
            # Если rate limiter активен — он сам рулит паузами.
            # Иначе оставляем старый предохранитель от долбёжки.
            if _rps_for(spec) <= 0:
                time.sleep(0.15)

    if capped:
        note = f" из {reported_total} по данным API" if reported_total else ""
        print(f"\n  ✂️ [{label}] Срез по настройке max_records: взято первых "
              f"{len(all_data)}{note}.")
        print(f"✅ записей: {len(all_data)} (страниц: {page})")
        if treat_as_complete:
            # ПРИМЕНЯЕМ ПРЕПРОЦЕССОР: единый для всех источников,
            # включая те, что без split_param. Раньше он вызывался
            # только в ветке дробления.
            if getattr(spec, 'preprocess', None) and spec.preprocess in src.PREPROCESSORS:
                all_data = src.PREPROCESSORS[spec.preprocess](
                    {'items': all_data}, None)
            # ДЕДУП: API отдаёт один лот под разными клиент-каналами.
            if getattr(spec, 'id_field', None):
                _seen, _uniq = set(), []
                for _r in all_data:
                    _rid = _r.get(spec.id_field) if isinstance(_r, dict) else None
                    if _rid is None:
                        _uniq.append(_r); continue
                    _k = str(_rid)
                    if _k in _seen: continue
                    _seen.add(_k); _uniq.append(_r)
                all_data = _uniq
            return all_data, False, len(all_data)
        print(f"  ⚠️ [{label}] аварийный срез — запуск помечен неполным.")
        # ПРИМЕНЯЕМ ПРЕПРОЦЕССОР: единый для всех источников,
        # включая те, что без split_param. Раньше он вызывался
        # только в ветке дробления.
        if getattr(spec, 'preprocess', None) and spec.preprocess in src.PREPROCESSORS:
            all_data = src.PREPROCESSORS[spec.preprocess](
                {'items': all_data}, None)
        # ДЕДУП: API отдаёт один лот под разными клиент-каналами.
        if getattr(spec, 'id_field', None):
            _seen, _uniq = set(), []
            for _r in all_data:
                _rid = _r.get(spec.id_field) if isinstance(_r, dict) else None
                if _rid is None:
                    _uniq.append(_r); continue
                _k = str(_rid)
                if _k in _seen: continue
                _seen.add(_k); _uniq.append(_r)
            all_data = _uniq
        return all_data, True, reported_total

    if reported_total is not None and len(all_data) < reported_total and not stalled:
        incomplete = True
        print(f"\n  ⚠️ [{label}] собрано {len(all_data)} из заявленных "
              f"{reported_total} — не хватает {reported_total - len(all_data)}.")

    print(f"{'⚠️ ЧАСТИЧНО' if incomplete else '✅'} записей: {len(all_data)}"
          f" (страниц: {page})")
    # ПРИМЕНЯЕМ ПРЕПРОЦЕССОР: единый для всех источников,
    # включая те, что без split_param. Раньше он вызывался
    # только в ветке дробления.
    if getattr(spec, 'preprocess', None) and spec.preprocess in src.PREPROCESSORS:
        all_data = src.PREPROCESSORS[spec.preprocess](
            {'items': all_data}, None)
    # ДЕДУП: API отдаёт один лот под разными клиент-каналами.
    if getattr(spec, 'id_field', None):
        _seen, _uniq = set(), []
        for _r in all_data:
            _rid = _r.get(spec.id_field) if isinstance(_r, dict) else None
            if _rid is None:
                _uniq.append(_r); continue
            _k = str(_rid)
            if _k in _seen: continue
            _seen.add(_k); _uniq.append(_r)
        all_data = _uniq
    return all_data, incomplete, reported_total


async def _fetch_json(url, session, label, page, max_attempts=None, spec=None):
    status, body_snippet = None, None
    attempts = max_attempts or MAX_RETRIES
    for attempt in range(1, attempts + 1):
        raw = await src.async_fetch_raw(url, spec, session=session)
        if raw.get("error"):
            if raw.get("error_kind") == "circuit_open":
                logging.warning("[%s] стр.%d: %s",
                                label, page, raw["error"])
                return None, None, "circuit_open"
            logging.warning("[%s] стр.%d попытка %d/%d: %s",
                            label, page, attempt, attempts, raw["error"])
            if attempt < attempts:
                await asyncio.sleep(RETRY_DELAY_SEC * attempt)
                continue
            return None, status, body_snippet

        status = raw.get("status")
        if status is not None and status >= 400:
            body_snippet = (raw.get("body") or b"").decode(
                "utf-8", errors="replace")[:500]
            logging.warning("[%s] стр.%d попытка %d/%d: HTTP %d — %s",
                            label, page, attempt, attempts, status, body_snippet)
            if status in (401, 403) or _looks_deterministic(body_snippet):
                return None, status, body_snippet
            if attempt < attempts:
                backoff = RETRY_DELAY_SEC * attempt * (3 if status == 429 else 1)
                await asyncio.sleep(backoff)
            continue

        if spec is None:
            # Резервный путь без стратегии парсинга (совместимость с preflight)
            import json as _json
            try:
                return _json.loads((raw.get("body") or b"").decode(
                    "utf-8", errors="replace")), status, None
            except ValueError as e:
                return None, status, str(e)

        try:
            return src.parse_payload(raw.get("body"), spec), status, None
        except ValueError as e:
            logging.warning("[%s] стр.%d: не удалось разобрать ответ: %s",
                            label, page, e)
            return None, status, str(e)
    return None, status, body_snippet
