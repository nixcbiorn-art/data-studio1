"""scrape_browser — сбор через виртуальный браузер (HTML и XHR)."""
from __future__ import annotations

import asyncio
import json
import logging
from redcat.sources import registry as src
from redcat.collection.scrape_env import browser_fetch
from redcat.collection.scrape_pages import _set_query_param, find_total


# ──────────────────────────────────────────────────────────────
#  РЕЖИМ БРАУЗЕРА (для источников с fetch_mode="browser")
# ──────────────────────────────────────────────────────────────
def _extract_items_from_html(html, spec):
    """Ищет записи в HTML. Возвращает список dict-ов или None."""
    import re as _re
    m = _re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, _re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            found = src.dig(data, spec.data_path) if spec.data_path else None
            if isinstance(found, list):
                return found
        except (ValueError, KeyError):
            pass
    for m in _re.finditer(
            r'<script[^>]+type="application/json"[^>]*>(.+?)</script>',
            html, _re.S):
        try:
            data = json.loads(m.group(1))
            found = src.dig(data, spec.data_path) if spec.data_path else None
            if isinstance(found, list):
                return found
        except (ValueError, KeyError):
            continue
    try:
        data = json.loads(html)
        found = src.dig(data, spec.data_path) if spec.data_path else data
        if isinstance(found, list):
            return found
    except ValueError:
        pass
    return None


def _collect_source_browser_sync(spec, base_url):
    """Sync-обход одной вкладкой. Возвращает (rows, incomplete, total)."""
    is_offset = "offset" in (spec.page_number_param or "").lower()
    first_page = 0 if is_offset else 1
    all_rows: list = []
    seen_ids: set = set()
    page = first_page
    total = None
    empty_streak = 0
    max_pages = 1000
    stopped_reason = "?"

    try:
        with browser_fetch.BrowserFetcher(
                spec.key, headless=spec.browser_headless) as fetcher:
            while page - first_page < max_pages:
                url = _set_query_param(
                    _set_query_param(base_url, spec.page_size_param, spec.page_size),
                    spec.page_number_param, page)
                label = f"{spec.key}:p{page}"
                try:
                    data = fetcher.fetch_json(
                        url, wait_for=spec.browser_wait_for,
                        wait_ms=spec.browser_wait_ms)
                except Exception as e:  # noqa: BLE001
                    print(f"\n  ❌ [{label}] ошибка браузера: "
                          f"{type(e).__name__}: {e}")
                    stopped_reason = f"ошибка браузера на p{page}"
                    break

                if not isinstance(data, dict):
                    print(f"\n  ⚠️ [{label}] страница вернула не JSON "
                          f"(вероятно, Qrator-челлендж снова). Останавливаюсь.")
                    stopped_reason = f"не-JSON на p{page} (Qrator?)"
                    break

                if total is None:
                    total = find_total(data, spec)

                items = src.dig(data, spec.data_path)
                if not isinstance(items, list):
                    print(f"\n  ⚠️ [{label}] по пути {spec.data_path} нет списка")
                    stopped_reason = f"нет items на p{page}"
                    break

                fresh = 0
                for r in items:
                    rid = r.get(spec.id_field) if isinstance(r, dict) else None
                    if rid is None or str(rid) not in seen_ids:
                        all_rows.append(r)
                        fresh += 1
                        if rid is not None:
                            seen_ids.add(str(rid))

                print(f"  📡 [{label}] +{fresh} (всего: {len(all_rows)}"
                      + (f" из {total}" if total else "") + ")")

                if not items:
                    empty_streak += 1
                    if empty_streak >= 2:
                        stopped_reason = f"две пустых страницы подряд (p{page})"
                        break
                    page += spec.page_size if is_offset else 1
                    continue
                empty_streak = 0

                if total and len(all_rows) >= total:
                    stopped_reason = f"собрано всё ({len(all_rows)} из {total})"
                    break

                # Шаг всегда на spec.page_size, а не на len(items) — так мы
                # не пропустим куски, если API отдаёт страницы короче.
                page += spec.page_size if is_offset else 1

        print(f"  ℹ️ [{spec.key}] остановлено: {stopped_reason}")
    except Exception as e:  # noqa: BLE001
        print(f"\n  ❌ [{spec.key}] браузер упал: {type(e).__name__}: {e}")
        logging.exception("[%s] browser error", spec.key)
        return all_rows, True, total

    return all_rows, False, total


async def _fetch_pages_browser_async(spec, base_url, concurrency: int = 4):
    """Параллельный обход страниц через async-Playwright.

    Простая параллель по фиксированным offset работает только если
    сервер отдаёт страницы одного размера. У samolet.ru размер плавает
    (36, 30, 20...), поэтому здесь сделано иначе: диапазон [0, total]
    разбивается на `concurrency` сегментов, и внутри каждого обход идёт
    ПОСЛЕДОВАТЕЛЬНО, с offset += фактически полученное число записей.
    Сегменты обрабатываются параллельно, потерь на границах нет.
    """
    if not (getattr(browser_fetch, "_PW_ASYNC_AVAILABLE", False)
            and hasattr(browser_fetch, "AsyncBrowserFetcher")):
        return [], True, None

    is_offset = "offset" in (spec.page_number_param or "").lower()
    first_page = 0 if is_offset else 1

    async with browser_fetch.AsyncBrowserFetcher(
            spec.key, headless=spec.browser_headless) as fetcher:

        # 1. Первая страница — прогрев Qrator + узнаём total
        first_url = _set_query_param(
            _set_query_param(base_url, spec.page_size_param, spec.page_size),
            spec.page_number_param, first_page)
        first_data = await fetcher.fetch_json(
            first_url, wait_ms=spec.browser_wait_ms)
        if first_data is None:
            print(f"  ⚠️ [{spec.key}] первая страница не отдала JSON")
            return [], True, None

        first_items = src.dig(first_data, spec.data_path)
        if not isinstance(first_items, list):
            return [], True, None

        total = find_total(first_data, spec)
        all_rows: list = list(first_items)
        seen_ids = {str(r.get(spec.id_field)) for r in first_items
                    if isinstance(r, dict) and r.get(spec.id_field) is not None}
        print(f"  📡 [{spec.key}:p{first_page}] +{len(first_items)} "
              f"(всего: {len(all_rows)}"
              + (f" из {total}" if total else "") + ")")

        if not total or not is_offset:
            # Без total или без offset — откатываемся на обычный параллельный
            # обход по фиксированному шагу (для источников со стабильной
            # пагинацией он работает нормально).
            print(f"  ℹ️ [{spec.key}] total неизвестен — параллельный обход "
                  f"без гарантий полноты")
            return all_rows, True, total

        # 2. Разбиваем на сегменты и обходим каждый последовательно
        starts = [first_page + i * (total // concurrency)
                  for i in range(concurrency)]
        # первый сегмент уже частично пройден на первой странице
        starts[0] = first_page

        sem = asyncio.Semaphore(concurrency)
        lock = asyncio.Lock()

        async def _segment(start: int, end: int):
            offset = start
            while offset < end:
                url = _set_query_param(
                    _set_query_param(base_url, spec.page_size_param,
                                     spec.page_size),
                    spec.page_number_param, offset)
                data = await fetcher.fetch_json(
                    url, wait_ms=spec.browser_wait_ms)
                if data is None:
                    # пропуск страницы — Qrator или временный сбой
                    offset += spec.page_size
                    continue
                items = src.dig(data, spec.data_path)
                if not isinstance(items, list) or not items:
                    break
                async with lock:
                    for r in items:
                        rid = r.get(spec.id_field) if isinstance(r, dict) else None
                        if rid is not None:
                            k = str(rid)
                            if k in seen_ids:
                                continue
                            seen_ids.add(k)
                        all_rows.append(r)
                # шагаем по фактическому размеру страницы — это главное
                offset += len(items) if is_offset else 1

        async def _one(start, end):
            async with sem:
                await _segment(start, end)

        tasks = []
        for i in range(concurrency):
            s = starts[i]
            e = starts[i + 1] if i + 1 < concurrency else total
            tasks.append(asyncio.create_task(_one(s, e)))

        done = 0
        for fut in asyncio.as_completed(tasks):
            await fut
            done += 1
            print(f"\r  📡 [{spec.key}] сегмент {done}/{concurrency} готов, "
                  f"записей: {len(all_rows)}", end="", flush=True)
        print()

    incomplete = bool(total and len(all_rows) < total * 0.99)
    return all_rows, incomplete, total


def _collect_source_browser_xhr(spec, params):
    """Сбор источника через перехват XHR.

    Открывает страницу, слушает XHR/fetch, берёт первый JSON-ответ,
    подходящий под spec.browser_xhr_pattern, парсит его стратегией
    spec.parse_strategy и возвращает записи. Постраничный обход тут
    не делается: если источник пагинируется, обычно все данные приходят
    одним XHR-ответом (или несколькими — тогда нужна отдельная стратегия
    обхода, которая добавится позже).
    """
    try:
        base_url = spec.resolved_url(params)
    except ValueError as e:
        print(f"  ❌ [{spec.key}] {e}")
        return [], True, None

    if browser_fetch is None:
        print(f"  ❌ [{spec.key}] fetch_mode=browser, но browser_fetch.py "
              f"не найден или Playwright не установлен.")
        return [], True, None

    mode = "видимое окно" if not spec.browser_headless else "headless"
    print(f"  🌐 [{spec.key}] browser_xhr ({mode}); "
          f"паттерн: {spec.browser_xhr_pattern or '(любой JSON)'}")

    raw = src.fetch_raw(base_url, spec)   # fetcher открывается/закрывается сам
    if raw.get("error"):
        print(f"  ❌ [{spec.key}] XHR: {raw['error']}")
        return [], True, None

    try:
        parsed = src.parse_payload(raw.get("body"), spec)
    except ValueError as e:
        print(f"  ❌ [{spec.key}] ответ XHR не разобран: {e}")
        return [], True, None

    items = parsed.get("items") or []
    total = parsed.get("total")
    print(f"  ✅ [{spec.key}] XHR: записей {len(items)}"
          + (f" из {total}" if total else ""))
    incomplete = bool(total and len(items) < total)
    return items, incomplete, total


def collect_source_browser(spec, session, token, params, collected):
    """Сбор источника через Playwright.

    Если concurrency > 1 — параллельный async-обход; Qrator-челлендж
    проходится один раз, cookie общая. При concurrency == 1 — sync-обход
    одной вкладкой.
    """
    if browser_fetch is None:
        print(f"  ❌ [{spec.key}] fetch_mode=browser, но browser_fetch.py "
              f"не найден или Playwright не установлен.")
        return [], True, None

    try:
        base_url = spec.resolved_url(params)
    except ValueError as e:
        print(f"  ❌ [{spec.key}] {e}")
        return [], True, None

    concurrency = max(1, int(getattr(spec, "concurrency", 1) or 1))
    mode = "видимое окно" if not spec.browser_headless else "headless"
    print(f"  🌐 [{spec.key}] браузер ({mode}), параллельность {concurrency}")
    logging.info("[%s] browser mode: %s, concurrency=%d",
                 spec.key, base_url, concurrency)

    has_async = bool(getattr(browser_fetch, "_PW_ASYNC_AVAILABLE", False))
    has_async_fetcher = hasattr(browser_fetch, "AsyncBrowserFetcher")

    if concurrency <= 1 or not (has_async and has_async_fetcher):
        if not (has_async and has_async_fetcher):
            print(f"  ℹ️ [{spec.key}] async Playwright недоступен — "
                  f"обход пойдёт по одной странице (sync). Для параллельного "
                  f"обхода добавьте AsyncBrowserFetcher в browser_fetch.py.")
        return _collect_source_browser_sync(spec, base_url)

    try:
        rows, incomplete, total = asyncio.run(
            _fetch_pages_browser_async(spec, base_url, concurrency=concurrency))
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ [{spec.key}] async-обход упал: {type(e).__name__}: {e}")
        logging.exception("[%s] async browser error", spec.key)
        return [], True, None

    print(f"  ✅ [{spec.key}] всего собрано: {len(rows)}"
          + (f" из {total}" if total else ""))
    return rows, incomplete, total
