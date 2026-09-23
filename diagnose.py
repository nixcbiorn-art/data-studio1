"""
ДИАГНОСТИКА ИСТОЧНИКА
=====================
Отвечает на вопрос «почему собралось меньше, чем должно» — по шагам, на
реальных запросах, но дёшево: несколько GET-ов вместо полного обхода.

    python redcat_scraper.py --diagnose apartments
    python redcat_scraper.py --diagnose            (все источники)

Проверяется вся цепочка, на каждом звене которой данные могут потеряться
молча:

  1. URL собирается (все плейсхолдеры подставлены).
  2. Запрос проходит, ответ — валидный JSON.
  3. Массив записей лежит там, где указано в data_path.
  4. Общее число записей находится (без него полноту не с чем сверять).
  5. Есть ссылка на следующую страницу — и она абсолютная.
  6. Страница 2 действительно отличается от страницы 1.
  7. Размер страницы соблюдается сервером.
  8. Для дробящихся источников — фильтр реально фильтрует.
  9. Оценка: сколько страниц нужно и упрёмся ли в лимит окна пагинации.
 10. Сверка с тем, что уже лежит в локальной базе.

Ничего не пишет и не меняет: только GET-запросы (см. api_guard.py).
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
from pathlib import Path

import sources as src

OK, WARN, BAD, INFO = "✅", "⚠️", "❌", "ℹ️"

# Типичный потолок окна пагинации у Elasticsearch-бэкендов
ES_WINDOW = 10000


class Report:
    """Копит строки проверки и итоговый вердикт."""

    def __init__(self, key):
        self.key = key
        self.lines = []
        self.problems = []
        self.advice = []

    def add(self, level, title, detail=""):
        self.lines.append({"level": level, "title": title, "detail": detail})
        if level == BAD:
            self.problems.append(title)

    def tip(self, text):
        self.advice.append(text)

    @property
    def verdict(self):
        if self.problems:
            return BAD, "есть поломки — данные будут неполными"
        if any(l["level"] == WARN for l in self.lines):
            return WARN, "работает, но есть риски"
        return OK, "всё в порядке"

    def to_dict(self):
        level, text = self.verdict
        return {"key": self.key, "level": level, "verdict": text,
                "lines": self.lines, "advice": self.advice}

    def print(self):
        level, text = self.verdict
        print(f"\n{'─' * 62}")
        print(f"{level} ИСТОЧНИК «{self.key}» — {text}")
        print("─" * 62)
        for line in self.lines:
            print(f"  {line['level']} {line['title']}")
            if line["detail"]:
                for part in str(line["detail"]).split("\n"):
                    print(f"      {part}")
        if self.advice:
            print("\n  Что делать:")
            for i, tip in enumerate(self.advice, 1):
                for j, part in enumerate(tip.split("\n")):
                    print(f"      {str(i) + '.' if j == 0 else '  '} {part}")


def _get(session, url, timeout=25):
    """GET с замером времени. Возвращает (данные, статус, мс, ошибка)."""
    started = time.perf_counter()
    try:
        resp = session.get(url, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — показываем пользователю как есть
        return None, None, 0, f"{type(e).__name__}: {e}"
    ms = round((time.perf_counter() - started) * 1000)
    if resp.status_code >= 400:
        return None, resp.status_code, ms, resp.text[:400]
    try:
        return resp.json(), resp.status_code, ms, None
    except ValueError:
        return None, resp.status_code, ms, f"ответ не JSON: {resp.text[:200]}"


def _local_count(data_db, key):
    """Сколько записей этого источника лежит в локальной базе."""
    path = Path(data_db)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (key,)).fetchone()
        if not row[0]:
            conn.close()
            return None
        count = conn.execute(f'SELECT COUNT(*) FROM "{key}"').fetchone()[0]
        conn.close()
        return count
    except sqlite3.Error:
        return None


def diagnose_source(spec, session, params, data_db=None, find_total=None,
                    collected_parents=None):
    """Полная проверка одного источника. Возвращает Report."""
    rep = Report(spec.key)

    # ── 1. URL ──
    try:
        base_url = spec.resolved_url(params)
        rep.add(OK, "URL собран", base_url[:150])
    except ValueError as e:
        rep.add(BAD, "URL не собран", str(e))
        rep.tip("Добавьте недостающие значения в .env (REDCAT_REGION_ID и т.п.).")
        return rep

    sep = "&" if "?" in base_url else "?"
    page1_url = (f"{base_url}{sep}{spec.page_size_param}={spec.page_size}"
                 f"&{spec.page_number_param}=1")

    # ── 2. Запрос ──
    data, status, ms, error = _get(session, page1_url)
    if data is None:
        rep.add(BAD, f"Запрос не прошёл (HTTP {status})", error)
        if status in (401, 403):
            rep.tip("Токен не даёт доступа к этому эндпоинту. Обновите токен или "
                    "проверьте права учётной записи.")
        elif status == 404:
            rep.tip("Адрес не найден. Сверьте путь эндпоинта с документацией API.")
        else:
            rep.tip("Посмотрите тело ответа выше — API обычно пишет там причину.")
            _bisect_and_report(rep, session, page1_url, spec)
        return rep
    rep.add(OK, f"Запрос прошёл (HTTP {status}, {ms} мс)")

    # ── 3. Массив записей ──
    items = src.dig(data, spec.data_path)
    if not isinstance(items, list):
        guess = src._find_first_list_path(data)
        rep.add(BAD, f"По пути data_path={list(spec.data_path)} нет списка записей",
                f"Там лежит: {type(items).__name__}")
        if guess is not None:
            found = src.dig(data, guess)
            rep.add(INFO, f"Похоже, записи лежат по пути {guess}",
                    f"там {len(found)} элементов")
            rep.tip(f'Впишите в sources/*.json:  "data_path": {json.dumps(guess)}')
        else:
            rep.tip("В ответе не нашлось ни одного списка объектов. "
                    "Возможно, эндпоинт отдаёт одну запись, а не выборку.")
        return rep
    rep.add(OK, f"Записи найдены: {len(items)} на первой странице")

    if not items:
        rep.add(WARN, "Первая страница пустая",
                "Фильтры в URL могли отсечь всё. Проверьте region_id/country_id.")

    # ── 4. Общее число ──
    total = find_total(data, spec) if find_total else src.dig(data, spec.total_path)
    if total is None:
        rep.add(BAD, "В ответе нет общего числа записей",
                f"Искали по {list(spec.total_path)} и по типовым путям.")
        rep.tip("Без общего числа полноту сбора не с чем сверять — потеря половины\n"
                "данных пройдёт незамеченной. Найдите total в ответе API и укажите\n"
                'его в "total_path".')
    else:
        rep.add(OK, f"API заявляет всего записей: {total}")

    # ── 5. Ссылка на следующую страницу ──
    next_link = src.dig(data, spec.next_link_path)
    if not next_link:
        if total and total > len(items):
            rep.add(WARN, "Ссылки на следующую страницу нет",
                    f"next_link_path={list(spec.next_link_path)} пуст, "
                    f"а записей заявлено {total}.")
            rep.tip("Обход пойдёт по номерам страниц — это поддерживается.\n"
                    "Если у API ссылка лежит в другом месте, укажите next_link_path —\n"
                    "так надёжнее.")
        else:
            rep.add(OK, "Ссылки на следующую страницу нет — данные помещаются на одну")
    elif not str(next_link).lower().startswith(("http://", "https://")):
        rep.add(WARN, "Ссылка на следующую страницу относительная",
                f"{next_link}")
        rep.add(INFO, "Она достраивается до абсолютной автоматически",
                "Раньше на этом месте обход обрывался после первой страницы.")
    else:
        rep.add(OK, "Ссылка на следующую страницу есть и абсолютная")

    # ── 6. Размер страницы ──
    if len(items) not in (spec.page_size, total or spec.page_size):
        if total and len(items) < min(spec.page_size, total):
            rep.add(WARN, f"Сервер отдал {len(items)} записей вместо "
                          f"{spec.page_size}",
                    "Похоже, у API свой потолок размера страницы.")
            rep.tip(f'Это не ошибка, но страниц будет больше. Можно выставить\n'
                    f'"page_size": {len(items)}, чтобы счётчики сходились.')

    # ── 7. Вторая страница действительно другая ──
    if total and total > len(items):
        second_url = (f"{base_url}{sep}{spec.page_size_param}={spec.page_size}"
                      f"&{spec.page_number_param}=2")
        data2, status2, ms2, err2 = _get(session, second_url)
        if data2 is None:
            rep.add(BAD, f"Вторая страница не открылась (HTTP {status2})", err2)
            rep.tip("Если тело ответа упоминает result window — API не отдаёт\n"
                    "глубокие страницы, запрос надо дробить (split_param).")
        else:
            items2 = src.dig(data2, spec.data_path) or []
            ids1 = {str(r.get(spec.id_field)) for r in items
                    if isinstance(r, dict)}
            ids2 = {str(r.get(spec.id_field)) for r in items2
                    if isinstance(r, dict)}
            if ids1 and ids2 and ids2 <= ids1:
                rep.add(BAD, "Вторая страница повторяет первую",
                        f"API игнорирует «{spec.page_number_param}».")
                rep.tip("Уточните в документации, как у этого API устроена\n"
                        "пагинация: возможно, нужны limit/offset или курсор.")
            elif not items2:
                rep.add(WARN, "Вторая страница пустая",
                        f"хотя всего заявлено {total}")
            else:
                rep.add(OK, f"Пагинация работает: страница 2 содержит "
                            f"{len(items2)} других записей")

    # ── 8. Оценка объёма и лимита окна ──
    if total:
        per_page = len(items) or spec.page_size
        pages = -(-total // per_page)
        rep.add(INFO, f"Потребуется страниц: ~{pages} "
                      f"(по {per_page} записей)")
        if spec.max_records and not spec.split_param:
            rep.add(INFO, f"Режим среза: берутся первые {spec.max_records} из {total}",
                    "Это настройка max_records, а не поломка. Порядок задаёт sort в URL.")
        elif total > ES_WINDOW and not spec.split_param:
            rep.add(BAD, f"Записей больше лимита окна пагинации (~{ES_WINDOW})",
                    f"Постраничный обход остановится примерно на {ES_WINDOW}, "
                    f"а нужно {total}.")
            rep.tip("Запрос нужно дробить: добавьте в описание источника\n"
                    '"split_param" (поле фильтра), "split_values_from" (источник\n'
                    'значений) и "split_values_field". См. README, раздел\n'
                    '«Обход лимита пагинации».')

    # ── 9. Проверка дробления ──
    if spec.split_param:
        _diagnose_split(rep, spec, session, base_url, sep, total, collected_parents)

    # ── 10. Сверка с локальной базой ──
    if data_db:
        local = _local_count(data_db, spec.key)
        if local is None:
            rep.add(INFO, "В локальной базе этого источника ещё нет")
        elif total:
            share = 100.0 * local / total
            level = OK if share >= 99 else (WARN if share >= 80 else BAD)
            rep.add(level, f"В локальной базе: {local} из {total} ({share:.1f}%)")
            if share < 99:
                rep.tip("Соберите источник заново после исправлений:\n"
                        f"    python redcat_scraper.py --only {spec.key}")
        else:
            rep.add(INFO, f"В локальной базе: {local} записей "
                          f"(сверить не с чем — нет total)")
    return rep


def _bisect_and_report(rep, session, failing_url, spec):
    """Убирает параметры запроса по одному, чтобы найти виновника ошибки.

    Работает для любого источника, не только для «жилья» из этого проекта:
    берёт реальный неудавшийся URL, пробует его без каждого параметра
    фильтра/сортировки по очереди (пагинация не трогается) и смотрит, при
    отсутствии какого параметра запрос вдруг проходит. Каждая проба — один
    дешёвый GET с page[size]=1.

    Это единственный способ найти причину, когда API отвечает HTTP 500 без
    информативного тела: часто такое бывает, если бэкенд не валидирует
    параметр (незнакомое поле сортировки, не тот формат булева фильтра) и
    падает с необработанным исключением вместо понятного 400.
    """
    parts = urllib.parse.urlsplit(failing_url)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    skip = {spec.page_size_param, spec.page_number_param}
    candidates = [i for i, (k, _) in enumerate(pairs) if k not in skip]
    if len(candidates) < 2:
        return  # нечего сравнивать — виноват, видимо, сам путь или токен

    trials = []
    for i in candidates[:12]:  # разумный потолок на случай длинного URL
        trial_pairs = [(k, v) for j, (k, v) in enumerate(pairs) if j != i]
        trial_pairs = [(k, v) for k, v in trial_pairs if k not in skip]
        trial_pairs += [(spec.page_size_param, "1"), (spec.page_number_param, "1")]
        trial_url = urllib.parse.urlunsplit(
            parts._replace(query=urllib.parse.urlencode(trial_pairs)))
        data, status, _ms, _err = _get(session, trial_url)
        trials.append({"param": f"{pairs[i][0]}={pairs[i][1]}",
                       "ok": data is not None, "status": status})

    fixed_by = [t for t in trials if t["ok"]]
    if fixed_by:
        rep.add(BAD, "Без одного из параметров запрос проходит — вот виновник",
                "\n".join(f"без «{t['param']}» → HTTP {t['status']} (успех)"
                         for t in fixed_by))
        rep.tip("Вероятная причина — именно этот параметр:\n    "
                + "\n    ".join(t["param"] for t in fixed_by)
                + "\nПроверьте его написание и формат в документации API: "
                  "часто дело в названии поля сортировки (может быть не то, "
                  "что в БД) или в формате булева значения (true/1, false/0).")
    else:
        rep.add(INFO, "Поочерёдное удаление параметров не помогло",
                f"Проверено параметров: {len(trials)}. Ни один не является "
                f"единственной причиной.")
        rep.tip("Проблема, скорее всего, не в отдельном фильтре — проверьте "
                "сам путь эндпоинта, версию API (/v1/ vs /v2/) или права "
                "токена на этот конкретный ресурс.")


def _diagnose_split(rep, spec, session, base_url, sep, total, collected_parents):
    """Проверяет, что параметр дробления действительно фильтрует выдачу."""
    parent_values = []
    if collected_parents:
        # «developer.id» — вложенное поле сырой записи, а не ключ верхнего уровня
        parent_values = [src.dig(r, tuple(spec.split_values_field.split(".")))
                         if isinstance(r, dict) else None
                         for r in collected_parents.get(spec.split_values_from, [])]
        parent_values = [v for v in parent_values if v is not None][:5]

    if not parent_values:
        rep.add(WARN, f"Нет значений для дробления",
                f"Источник-родитель «{spec.split_values_from}» не собран — "
                f"проверить фильтр не на чем.")
        rep.tip(f"Сначала продиагностируйте родителя:\n"
                f"    python redcat_scraper.py --diagnose {spec.split_values_from}")
        return

    results = []
    for value in parent_values:
        url = (f"{base_url}{sep}{spec.split_param}={urllib.parse.quote(str(value))}"
               f"&{spec.page_size_param}=1&{spec.page_number_param}=1")
        data, status, ms, error = _get(session, url)
        if data is None:
            results.append({"value": value, "status": status, "error": error,
                            "total": None, "rows": None})
            continue
        rows = src.dig(data, spec.data_path)
        results.append({
            "value": value, "status": status, "error": None,
            "rows": len(rows) if isinstance(rows, list) else None,
            "total": src.dig(data, spec.total_path),
        })

    ok_results = [r for r in results if r["error"] is None]
    detail = "\n".join(
        f"{spec.split_param}={r['value']} → HTTP {r['status']}, "
        + (f"total={r['total']}" if r["error"] is None else f"ошибка: {r['error'][:90]}")
        for r in results)

    if not ok_results:
        rep.add(BAD, f"Фильтр «{spec.split_param}» не отработал ни разу", detail)
        rep.tip("Попробуйте другие написания split_param по очереди:\n"
                f'    "{spec.split_param}[]"\n'
                f'    "filter[{spec.split_values_field}]"\n'
                f'    "{spec.split_values_field}"')
        return

    totals = [r["total"] for r in ok_results if r["total"] is not None]

    if total and totals and all(t == total for t in totals):
        rep.add(BAD, f"Фильтр «{spec.split_param}» игнорируется", detail
                + f"\nБез фильтра тоже {total} — выдача не меняется.")
        rep.tip("Это самый неприятный случай: запросы проходят, ошибок нет,\n"
                "но дробление не работает и обход соберёт мусор. Нужен другой\n"
                "параметр фильтра — сверьтесь с документацией API.")
        return

    empty = [r for r in ok_results if not r["rows"]]
    if len(empty) == len(ok_results):
        rep.add(BAD, f"Под каждое значение приходит 0 записей", detail)
        rep.tip(f"Либо эндпоинт не умеет фильтровать по "
                f"«{spec.split_values_field}», либо ожидает другой идентификатор\n"
                f"(например, внешний код вместо внутреннего id).")
        return

    rep.add(OK, f"Фильтр «{spec.split_param}» работает",
            f"из {len(ok_results)} проб с данными {len(ok_results) - len(empty)}")

    if totals and total:
        biggest = max(totals)
        if biggest > ES_WINDOW:
            rep.add(WARN, f"На одно значение приходится до {biggest} записей",
                    f"это больше лимита окна пагинации (~{ES_WINDOW})")
            rep.tip("Дробите мельче: добавьте второй уровень (например, ещё и по\n"
                    "типу объекта или диапазону цены).")
