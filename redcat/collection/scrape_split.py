"""scrape_split — сбор по значениям (split): прогон, пакетная выборка, предпроверка."""
from __future__ import annotations

import asyncio
import logging
from redcat.sources import registry as src
from redcat.collection.scrape_config import REQUEST_TIMEOUT
from redcat.collection.scrape_env import aiohttp
from redcat.collection.scrape_pages import _fetch_json, _next_url, _set_query_param, find_total


ES_WINDOW = 10_000


def _get_field(row, field):
    if not isinstance(row, dict) or not field:
        return None
    return src.dig(row, tuple(field.split("."))) if "." in field else row.get(field)


def _norm_name(v):
    return " ".join(str(v).split()).casefold() if v not in (None, "") else None


def _total_of(data, spec):
    """Общее число записей из ответа _fetch_json (unified или сырой JSON)."""
    if not isinstance(data, dict):
        return None
    if "items" in data and "raw" in data:
        total = data.get("total")
        if isinstance(total, int) and not isinstance(total, bool):
            return total
        raw = data.get("raw")
        return find_total(raw, spec) if raw is not None else None
    return find_total(data, spec)


def _rows_of(data, spec):
    """Список записей из ответа _fetch_json (unified или сырой JSON)."""
    if not isinstance(data, dict):
        return None
    if "items" in data and "raw" in data:
        items = data.get("items")
    else:
        items = src.dig(data, spec.data_path)
    return items if isinstance(items, list) else None


async def preflight_check_split(base_url, sample_values, token, spec):
    """Быстрая проверка параметра дробления на нескольких значениях.

    Возвращает (ok, текст). ok=True, если фильтр реально фильтрует выдачу.
    """
    if not sample_values:
        return True, None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    sep = "&" if "?" in base_url else "?"
    results = []

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        # 1. Без фильтра: сколько всего записей в регионе.
        unfiltered_url = (f"{base_url}{sep}{spec.page_size_param}=1"
                          f"&{spec.page_number_param}=1")
        base_data, _, _ = await _fetch_json(
            unfiltered_url, session, f"preflight:{spec.key}", 0, spec=spec)
        unfiltered_total = _total_of(base_data, spec)

        # 2. Несколько значений фильтра. Два сбоя подряд — прекращаем.
        failed_in_row = 0
        for value in sample_values:
            url = (f"{base_url}{sep}{spec.split_param}={value}"
                   f"&{spec.page_size_param}={spec.page_size}"
                   f"&{spec.page_number_param}=1")
            data, status, body = await _fetch_json(
                url, session, f"preflight:{spec.key}", 1,
                max_attempts=1, spec=spec)
            rows = _rows_of(data, spec)
            results.append({
                "value": value, "ok": data is not None,
                "status": status, "body": body,
                "rows": len(rows) if rows is not None else None,
                "total": _total_of(data, spec),
            })
            failed_in_row = 0 if data is not None else failed_in_row + 1
            if failed_in_row >= 2:
                break

    responded = [r for r in results if r["ok"]]
    if not responded:
        lines = [f"Проверка на {len(results)} значениях провалилась целиком:"]
        for r in results:
            lines.append(f"    • {spec.split_param}={r['value']} → HTTP {r['status']}"
                         + (f": {r['body']}" if r["body"] else " (без тела ответа)"))
        return False, "\n".join(lines)

    # Фильтр игнорируется: с ним столько же записей, сколько без него.
    if unfiltered_total:
        ignored = [r for r in responded
                   if r["total"] is not None and r["total"] == unfiltered_total]
        if len(ignored) == len(responded):
            return False, (
                f"Фильтр «{spec.split_param}» не влияет на выдачу: и без него, и с\n"
                f"    любым из {len(responded)} проверенных значений API отдаёт одни и те же\n"
                f"    {unfiltered_total} записей.")

    empty = [r for r in responded if not r["rows"]]
    if len(empty) == len(responded):
        return False, (
            f"Все {len(responded)} проверенных значений вернули 0 записей без ошибок.")

    with_data = len(responded) - len(empty)
    return True, (f"фильтр работает: из {len(responded)} проб с данными {with_data}"
                  + (f", всего по региону {unfiltered_total}" if unfiltered_total else ""))


async def _fetch_one_split(value, session, semaphore, spec, base_url):
    label = f"{spec.key}:{value}"
    sep = "&" if "?" in base_url else "?"
    if spec.split_url_template:
        # Подставляем {value} и любые REDCAT_*-плейсхолдеры из окружения.
        # Нужно, например, для show-apartments-data, которому обязателен
        # filter[region_id]={region_id} — без него API отвечает HTTP 422.
        import os as _os
        raw_url = spec.split_url_template.replace("{value}", str(value))
        for _name, _val in _os.environ.items():
            if _name.startswith("REDCAT_"):
                _key = _name[len("REDCAT_"):].lower()
                raw_url = raw_url.replace("{" + _key + "}", str(_val))
        # подстраховка на случай, если REDCAT_REGION_ID не задан в .env
        if "{region_id}" in raw_url:
            raw_url = raw_url.replace("{region_id}",
                                      _os.environ.get("REDCAT_REGION_ID",
                                                      "20003956"))
        if "{country_id}" in raw_url:
            raw_url = raw_url.replace("{country_id}",
                                      _os.environ.get("REDCAT_COUNTRY_ID",
                                                      "2017370"))
        url = raw_url
        if spec.page_size_param:
            url = _set_query_param(url, spec.page_size_param, spec.page_size)
        if spec.page_number_param:
            url = _set_query_param(
                url, spec.page_number_param,
                0 if "offset" in (spec.page_number_param or "").lower() else 1)
    else:
        url = (f"{base_url}{sep}{spec.split_param}={value}"
               f"&{spec.page_size_param}={spec.page_size}"
               f"&{spec.page_number_param}=1")
    rows, page, incomplete, total, last_status = [], 1, False, None, None
    # Счётчик СЫРЫХ записей (до препроцессора). Нужен для корректной
    # остановки пагинации: препроцессор может выбрасывать часть
    # записей (osnova_flats убирает нежилые), и если сравнивать с
    # total по длине уже отфильтрованного списка, условие
    # `collected >= total` не выполняется никогда — цикл крутится,
    # пока сервер не начнёт отдавать пустые страницы. Именно так
    # osnova_apartments дошёл до page=304.
    fetched_raw = 0
    # seen_ids — защита от зацикливания: если API игнорирует page[number]
    # для какого-то значения и всегда возвращает одни и те же записи,
    # без этой проверки цикл уходит в тысячи запросов.
    seen_ids: set = set()

    async with semaphore:
        while url:
            data, status, _body = await _fetch_json(
                url, session, label, page,
                max_attempts=2 if page == 1 else None, spec=spec)
            if status is not None:
                last_status = status
            if data is None:
                if page == 1 and status is not None and status >= 500:
                    break
                incomplete = True
                break

            # Считаем СЫРЫЕ записи (до препроцессора). Если сравнивать
            # с total по отфильтрованной длине, условие `>= total`
            # не сработает никогда — цикл крутится до пустых страниц.
            raw_items = data.get("items") or []
            if page > 1 and not raw_items:
                break  # пустая страница — данные кончились
            if page > 300:
                logging.warning(
                    "[%s] больше 300 страниц — аварийная остановка, "
                    "сбор помечен неполным", label)
                incomplete = True
                break
            fetched_raw += len(raw_items)
            if spec.preprocess and spec.preprocess in src.PREPROCESSORS:
                items = src.PREPROCESSORS[spec.preprocess](data.get("raw"), value)
            else:
                items = raw_items
            if not isinstance(items, list):
                logging.warning("[%s] стр.%d: в ответе нет списка записей.",
                                label, page)
                break

            # Проверка повторов: если страница целиком состоит из уже
            # виденных id — фильтр игнорируется, обход значения нужно
            # остановить. Иначе крутим одну и ту же выдачу сотни раз.
            page_ids = {str(it.get(spec.id_field))
                        for it in items
                        if isinstance(it, dict) and it.get(spec.id_field) is not None}
            if page_ids and page > 1 and page_ids <= seen_ids:
                logging.warning(
                    "[%s] стр.%d: все id уже встречались — API игнорирует "
                    "пагинацию для этого значения. Прекращаю обход значения.",
                    label, page)
                break
            seen_ids |= page_ids

            rows.extend(items)
            if total is None:
                total = data.get("total")
            if total is None:
                total = find_total(data.get("raw"), spec)
            # fetched_raw (а не len(rows)) — иначе условие останова
            # никогда не сработает, если препроцессор отбрасывает часть
            # записей. В _next_url передаём размер СЫРОЙ страницы и
            # накопленное СЫРОЕ количество — пагинатор сравнивает с
            # total, заявленным API, а не с числом отфильтрованных.
            if fetched_raw >= ES_WINDOW and total is not None and total > fetched_raw:
                url = None
            else:
                url = _next_url(data.get("raw"), spec, url, page,
                                len(raw_items), fetched_raw, total)
            if url:
                page += 1
                await asyncio.sleep(0.05)

    # Сравниваем СЫРОЕ количество с total: препроцессор мог законно
    # выбросить часть записей (нежилые), и это не недобор.
    if total is not None and fetched_raw < total:
        logging.warning(
            "[%s] собрано %d из %d (сырых) — похоже, лимит пагинации "
            "достигнут даже для одного значения split.",
            label, fetched_raw, total)
        incomplete = True
    return rows, incomplete, last_status, total


async def fetch_split_async(base_url, values, token, spec):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    # Явный connect + sock_read: если сервер открыл соединение и молчит —
    # оборвём через sock_read секунд, а не будем висеть до total.
    timeout = aiohttp.ClientTimeout(
        total=REQUEST_TIMEOUT * 3,
        connect=REQUEST_TIMEOUT,
        sock_read=REQUEST_TIMEOUT,
    )
    semaphore = asyncio.Semaphore(spec.concurrency)
    out, any_incomplete, done, failed, empty_ok, suspect = [], False, 0, 0, 0, 0
    status_counts = {}
    _reported_total_sum = 0  # сумма total по всем split-значениям
    _reported_total_n = 0    # сколько раз total пришёл
    total = len(values)

    print(f"  📡 [{spec.key}] дробление по {spec.split_param}: "
          f"{total} значений, параллельность {spec.concurrency}")

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        tasks = [asyncio.create_task(
            _fetch_one_split(v, session, semaphore, spec, base_url)) for v in values]
        for coro in asyncio.as_completed(tasks):
            rows, inc, status, _t = await coro
            if _t is not None:
                _reported_total_sum += _t
            out.extend(rows)
            any_incomplete |= inc
            done += 1
            if inc:
                failed += 1
                if status is not None:
                    status_counts[status] = status_counts.get(status, 0) + 1
            elif not rows:
                if status is not None and status >= 500:
                    suspect += 1
                else:
                    empty_ok += 1
            if done % 1 == 0 or done == total:
                print(f"\r  📡 [{spec.key}] {done}/{total} — записей: {len(out)}",
                      end="", flush=True)
    print()

    ok = total - failed - empty_ok - suspect
    print(f"  {'⚠️ ЧАСТИЧНО' if any_incomplete else '✅'} всего: {len(out)} записей. "
          f"Из {total} значений: с данными {ok}, пустых (без ошибки) {empty_ok}, "
          f"HTTP 5xx без данных {suspect}, с ошибкой {failed}.")
    if suspect:
        print(f"  ℹ️ У {suspect} значений сервер ответил HTTP 5xx без данных — "
              f"похоже на ЖК без лотов.")

    if failed:
        detail = ", ".join(f"HTTP {code}: {cnt}" for code, cnt in sorted(status_counts.items()))
        print(f"  ⚠️ Ошибки при обходе ({failed} из {total}): "
              f"{detail or 'без кода статуса — таймаут или обрыв сети'}.")
        if status_counts.get(429):
            print(f"  💡 HTTP 429 = превышен лимит частоты запросов. Уменьшите "
                  f"\"concurrency\" у источника '{spec.key}' в sources/*.json "
                  f"(сейчас {spec.concurrency}).")

    return out, any_incomplete, suspect, _reported_total_sum
