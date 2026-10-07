"""scrape_collect — сбор одного источника (выбор способа и обработка)."""
from __future__ import annotations

import asyncio
import logging
from redcat.collection.scrape_browser import _collect_source_browser_xhr, collect_source_browser
from redcat.collection.scrape_pages import fetch_pages
from redcat.collection.scrape_probe import probe_live_values
from redcat.collection.scrape_split import _get_field, fetch_split_async, preflight_check_split


def collect_source(spec, session, token, params, collected):
    """Собирает один источник целиком. Возвращает (сырые_записи, incomplete, total).

    Диспетчер:
      • fetch_mode="browser" — уходит в collect_source_browser (Playwright);
      • есть split_param и split_values_from — обходит по дроблению;
      • иначе — обычный HTTP GET с пагинацией.
    """
    if getattr(spec, "fetch_mode", "http") == "browser":
        if getattr(spec, "fetch_strategy", "") == "browser_xhr":
            return _collect_source_browser_xhr(spec, params)
        return collect_source_browser(spec, session, token, params, collected)

    url = spec.resolved_url(params)

    # Дробление запроса по значениям поля (обход лимита окна пагинации).
    if (spec.split_param or spec.split_url_template) and spec.split_values_from:
        parent = collected.get(spec.split_values_from)
        if not parent:
            logging.error("[%s] источник-родитель '%s' пуст — пропуск.",
                          spec.key, spec.split_values_from)
            print(f"  ⚠️ [{spec.key}] нет данных родителя "
                  f"'{spec.split_values_from}' — пропускаем.")
            return [], True, None

        values = {_get_field(r, spec.split_values_field) for r in parent
                  if _get_field(r, spec.split_values_field) is not None}
        values |= set(spec.split_values_extra or ())
        values = sorted(values, key=str)

        # Если URL для каждого значения собирается из шаблона
        # (split_url_template), то стандартные проверки неприменимы:
        # базовый spec.url у таких источников невалиден без id в path,
        # а split_param пустой. Пропускаем probe_total, probe_live_values
        # и preflight_check_split, идём сразу на сбор.
        if spec.split_url_template:
            total = None
            print(f"  ℹ️ [{spec.key}] используется split_url_template — "
                  f"пробные запросы пропущены")
        else:
            # probe_total без фильтров для split-источника
            # даёт total по всей стране — вводит в заблуждение.
            # Настоящая сумма собирается в fetch_split_async.
            total = None

            live = probe_live_values(url, session, spec, parent)
            sample = live[:3] or values[:min(5, len(values))]
            if live:
                print(f"  ℹ️ [{spec.key}] Проверочные значения: "
                      f"{', '.join(str(v) for v in live[:3])}")
            print(f"  🔎 [{spec.key}] Проверяю параметр дробления '{spec.split_param}' "
                  f"(несколько быстрых запросов)...")
            ok, diag = asyncio.run(preflight_check_split(url, sample, token, spec))
            if not ok:
                print(f"  ❌ [{spec.key}] Предпроверка '{spec.split_param}' не пройдена:")
                print(f"     {diag}")
                if spec.max_records:
                    print(f"  ↩️ [{spec.key}] Дробление недоступно — перехожу на срез: "
                          f"первые {spec.max_records} записей.")
                    logging.warning("[%s] дробление недоступно, собираю срез "
                                    "max_records=%s", spec.key, spec.max_records)
                    spec.collected_as_slice = True
                    return fetch_pages(url, session, spec, spec.key,
                                       treat_as_complete=False)
                print(f"  ⛔ Сбор источника '{spec.key}' остановлен.")
                logging.error("[%s] preflight провален:\n%s", spec.key, diag)
                return [], True, total
            if diag:
                print(f"  ✅ [{spec.key}] предпроверка пройдена — {diag}")

        rows, inc, suspect, _reported_total_sum = asyncio.run(
fetch_split_async(url, values, token, spec))

        # ДЕДУП: API отдаёт одну и ту же квартиру под разными каналами
        # (FSK / FSK_APP / DSK / DSK_ADVERTISE). В split-режиме каждая
        # копия приходит отдельно, нужно схлопнуть по id_field.
        _dedup_removed = 0
        if getattr(spec, "id_field", None) and rows:
            _seen, _uniq = set(), []
            for _r in rows:
                _rid = _r.get(spec.id_field) if isinstance(_r, dict) else None
                if _rid is None:
                    _uniq.append(_r)
                    continue
                _k = str(_rid)
                if _k in _seen:
                    continue
                _seen.add(_k)
                _uniq.append(_r)
            _dedup_removed = len(rows) - len(_uniq)
            if _dedup_removed:
                print(f"  🧹 [{spec.key}] убрано дублей по {spec.id_field}: "
                      f"{_dedup_removed}")
            rows = _uniq

        if suspect and total is None:
            inc = True
        elif suspect and total is not None and len(rows) >= total:
            pass
        elif suspect:
            inc = True

        # total от API считает копии по каналам; после дедупа rows меньше
        # total — это нормально, а не потеря данных.
        if total is not None and len(rows) < total and not _dedup_removed:
            share = 100.0 * len(rows) / total
            print(f"  собрано {len(rows)} из {total} ({share:.1f}%)")
            if share < 80:
                inc = True

        return rows, inc, total

    # Обычный HTTP GET с пагинацией.
    return fetch_pages(url, session, spec, spec.key)
