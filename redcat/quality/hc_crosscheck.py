"""
ПЕРЕКРЁСТНАЯ ПРОВЕРКА: ЖК из ДОБЫТЫХ квартир против справочника ЖК
==================================================================
Идентификаторы ЖК берутся из самих собранных квартир (поле housing_complex_id
в каждой записи), а не запрашиваются у API через фильтр. Сеть, токен и
`split_param` не нужны: проверка работает только с уже добытыми данными.

Что сверяется:

  1. Каждый ЖК, который встретился в квартирах, есть в справочнике ЖК.
  2. Внутри записи квартиры id и название ЖК согласованы с вложенным
     объектом housing_complex (housing_complex_id = housing_complex.id).
  3. Название, застройщик и срок сдачи у одного и того же ЖК совпадают в
     квартирах и в справочнике (даты приводятся к одному виду: в справочнике
     31.12.2028, в квартирах 2028-12-31).
  4. Минимальная цена: min_price_apartments из справочника против самой
     дешёвой квартиры этого ЖК в собранных данных.
  5. Покрытие: сколько ЖК справочника вообще представлено в квартирах. Для
     СРЕЗА (max_records) низкое покрытие — ожидаемо и не считается поломкой.

Про минимальную цену. Если квартиры собраны полностью или срезом,
отсортированным по возрастанию цены (sort=price_asc), то самая дешёвая
квартира ЖК гарантированно попадает в выборку, и расхождение по любую
сторону — реальное. Если порядок произвольный, дешёвая квартира могла не
попасть в выборку: тогда «в данных дороже, чем в справочнике» ничего не
доказывает и не считается расхождением. А «в данных ДЕШЕВЛЕ, чем заявлено
справочником» — расхождение всегда.

Запуск без сбора (по уже сохранённым данным):
    python -m redcat.quality.hc_crosscheck
    python -m redcat.quality.hc_crosscheck --apartments reports/apartments_20260917_1608.csv
"""

from __future__ import annotations

from redcat.core import paths
import argparse
import csv
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

CRITICAL, WARNING, INFO, OK = "critical", "warning", "info", "ok"
ICON = {CRITICAL: "🔴", WARNING: "⚠️", INFO: "ℹ️", OK: "✅"}

# Имена колонок в нормализованных таблицах (см. flatten_record в sources.py).
DEFAULT_FIELDS = {
    "fk": "housing_complex_id",              # квартиры → id ЖК
    "nested_id": "housing_complex.id",       # вложенный объект ЖК внутри квартиры
    "nested_name": "housing_complex.name",
    "apt_name": "housing_complex_name",
    "apt_dev": "developer_name",
    "apt_deadline": "housing_complex_deadline",
    "apt_price": "price",
    "dir_id": "id",                          # справочник ЖК
    "dir_name": "name",
    "dir_dev": "developer_name",
    "dir_deadline": "deadline",
    "dir_min": "min_price_apartments",
}


# ──────────────────────────────────────────────────────────────
#  Разбор значений
# ──────────────────────────────────────────────────────────────
def _id(v):
    """Идентификатор как строка: 535, '535' и '535.0' (так CSV хранит целые) — одно и то же."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("nan", "none", "null"):
        return None
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(str(v).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _txt(v):
    return " ".join(str(v).split()).casefold() if v is not None else ""


try:
    from redcat.sources.name_normalizer import normalize as _hc_norm
except ImportError:                      # модуль лежит не рядом — работаем как раньше
    _hc_norm = None


def _nm(v):
    """Название ЖК: «ЖК Скай» и «Скай» — одно и то же. Для застройщика не годится."""
    if _hc_norm is None:
        return _txt(v)
    return _hc_norm(v) or _txt(v)


def _date(v):
    """31.12.2028 и 2028-12-31 → date(2028, 12, 31); всё остальное → None."""
    if v is None:
        return None
    s = str(v).strip()
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})", s)
    if m:
        d, mo, y = map(int, m.groups())
    else:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
        if not m:
            return None
        y, mo, d = map(int, m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _money(n):
    return f"{n:,.0f}".replace(",", "\u00a0")


def _mode(values):
    """Самое частое непустое значение (для названия/застройщика/срока по лотам ЖК)."""
    cnt = Counter(v for v in values if v not in (None, "") and str(v).strip().lower() not in ("nan", "none"))
    return cnt.most_common(1)[0][0] if cnt else None


def _examples(items, n=5):
    shown = ", ".join(str(x) for x in items[:n])
    return shown + (f" и ещё {len(items) - n}" if len(items) > n else "")


# ──────────────────────────────────────────────────────────────
#  Сама сверка (чистая функция: списки словарей на входе, результат на выходе)
# ──────────────────────────────────────────────────────────────
def reconcile(apartments, directory, *, slice_mode=False, min_price_complete=False,
              tol=0.005, fields=None) -> dict:
    """Сверяет ЖК, найденные в квартирах, со справочником ЖК.

    slice_mode         — квартиры собраны срезом (max_records): низкое покрытие
                         справочника не считается поломкой.
    min_price_complete — самая дешёвая квартира каждого ЖК гарантированно в
                         выборке (полный сбор или срез с sort=price_asc).
    tol                — допуск при сравнении цен (0.005 = 0,5%).
    """
    f = {**DEFAULT_FIELDS, **(fields or {})}
    checks, rows = [], []

    dir_by_id = {}
    for r in directory:
        i = _id(r.get(f["dir_id"]))
        if i is not None:
            dir_by_id[i] = r

    groups = defaultdict(list)
    no_fk = id_mismatch = nested_checked = name_nested_bad = 0
    for a in apartments:
        i = _id(a.get(f["fk"]))
        if i is None:
            no_fk += 1
            continue
        groups[i].append(a)
        nid = _id(a.get(f["nested_id"]))
        if nid is not None:
            nested_checked += 1
            if nid != i:
                id_mismatch += 1
        nn, an = a.get(f["nested_name"]), a.get(f["apt_name"])
        if nn not in (None, "") and an not in (None, "") and _nm(nn) != _nm(an):
            name_nested_bad += 1

    n_apts = sum(len(v) for v in groups.values())
    ids_in_data = set(groups)
    dir_ids = set(dir_by_id)

    def add(level, text, detail=""):
        checks.append({"level": level, "text": text, "detail": detail})

    if not apartments:
        add(WARNING, "Квартир нет — сверять нечего.",
            "Соберите источник apartments или укажите файл: --apartments путь")
    if not directory:
        add(WARNING, "Справочник ЖК пуст — сверять не с чем.",
            "Соберите источник housing_complexes (запуск без --only).")
    if not apartments or not directory:
        return {"checks": checks, "rows": rows, "counts": _counts(checks),
                "summary": {"apartments": len(apartments), "hc_in_data": 0,
                            "hc_in_directory": len(dir_ids)}}

    # 0. квартиры без id ЖК
    if no_fk:
        add(WARNING, f"У {no_fk} квартир нет id ЖК — их не к чему привязать.")

    # 1. ЖК из квартир → справочник
    orphans = sorted(ids_in_data - dir_ids, key=lambda x: (len(x), x))
    if orphans:
        orphan_apts = sum(len(groups[i]) for i in orphans)
        share = 100.0 * orphan_apts / n_apts
        names = [f"{i} «{_mode(a.get(f['apt_name']) for a in groups[i]) or '?'}»" for i in orphans]
        add(CRITICAL if share > 5 else WARNING,
            f"{len(orphans)} ЖК из квартир нет в справочнике ({orphan_apts} квартир, {share:.1f}%)",
            "Примеры: " + _examples(names) + ". Справочник собран не полностью либо "
            "отфильтрован строже квартир (сверьте filter[...] в URL обоих источников).")
    else:
        add(OK, f"Все {len(ids_in_data)} ЖК из квартир найдены в справочнике.")

    # 2. согласованность внутри записи квартиры
    if nested_checked:
        if id_mismatch:
            add(CRITICAL, f"В {id_mismatch} квартирах housing_complex_id не равен вложенному housing_complex.id",
                "Запись описывает разные ЖК — данные API противоречат сами себе.")
        else:
            add(OK, f"Внутри квартир id ЖК согласован с вложенным объектом ({nested_checked} из {nested_checked}).")
    if name_nested_bad:
        add(WARNING, f"В {name_nested_bad} квартирах название ЖК расходится с вложенным housing_complex.name")

    # 3–4. по каждому ЖК: реквизиты и минимальная цена
    bad = {"name": [], "dev": [], "deadline": [], "multi": []}
    mp_compared = mp_match = mp_uncertain = 0
    mp_lower, mp_higher = [], []
    for i in sorted(ids_in_data, key=lambda x: (len(x), x)):
        lots = groups[i]
        d = dir_by_id.get(i)
        name_apt = _mode(a.get(f["apt_name"]) for a in lots)
        dev_apt = _mode(a.get(f["apt_dev"]) for a in lots)
        dl_apt = _mode(a.get(f["apt_deadline"]) for a in lots)
        prices = [p for p in (_num(a.get(f["apt_price"])) for a in lots) if p is not None and p > 0]
        min_apt = min(prices) if prices else None
        flags = []

        if len({_nm(a.get(f["apt_name"])) for a in lots if a.get(f["apt_name"]) not in (None, "")}) > 1:
            flags.append("у лотов разные названия ЖК")
            bad["multi"].append(i)

        name_dir = dev_dir = dl_dir = min_dir = None
        if d is None:
            flags.append("нет в справочнике")
        else:
            name_dir, dev_dir = d.get(f["dir_name"]), d.get(f["dir_dev"])
            dl_dir, min_dir = d.get(f["dir_deadline"]), _num(d.get(f["dir_min"]))
            if name_apt and name_dir and _nm(name_apt) != _nm(name_dir):
                flags.append("название"); bad["name"].append(f"{i} («{name_dir}» ≠ «{name_apt}»)")
            if dev_apt and dev_dir and _txt(dev_apt) != _txt(dev_dir):
                flags.append("застройщик"); bad["dev"].append(f"{i} («{dev_dir}» ≠ «{dev_apt}»)")
            da, dd = _date(dl_apt), _date(dl_dir)
            if da and dd and da != dd:
                flags.append("срок сдачи"); bad["deadline"].append(f"{i} ({dd:%d.%m.%Y} ≠ {da:%d.%m.%Y})")
            if min_dir and min_apt:
                mp_compared += 1
                if abs(min_dir - min_apt) <= tol * min_dir:
                    mp_match += 1
                elif min_apt < min_dir:
                    flags.append("мин. цена: в квартирах дешевле справочника")
                    mp_lower.append(f"{i} (справочник {_money(min_dir)}, в данных {_money(min_apt)})")
                elif min_price_complete:
                    flags.append("мин. цена: в квартирах дороже справочника")
                    mp_higher.append(f"{i} (справочник {_money(min_dir)}, в данных {_money(min_apt)})")
                else:
                    mp_uncertain += 1   # дешёвый лот мог не попасть в выборку — не расхождение
                    flags.append("мин. цена выше (выборка неполна — не расхождение)")

        rows.append({
            "hc_id": i, "name_directory": name_dir, "name_apartments": name_apt,
            "developer_directory": dev_dir, "developer_apartments": dev_apt,
            "deadline_directory": dl_dir, "deadline_apartments": dl_apt,
            "apartments_count": len(lots),
            "min_price_directory": min_dir, "min_price_apartments": min_apt,
            "flags": "; ".join(flags),
        })

    for i in sorted(dir_ids - ids_in_data, key=lambda x: (len(x), x)):
        d = dir_by_id[i]
        rows.append({
            "hc_id": i, "name_directory": d.get(f["dir_name"]), "name_apartments": None,
            "developer_directory": d.get(f["dir_dev"]), "developer_apartments": None,
            "deadline_directory": d.get(f["dir_deadline"]), "deadline_apartments": None,
            "apartments_count": 0, "min_price_directory": _num(d.get(f["dir_min"])),
            "min_price_apartments": None, "flags": "нет квартир в выборке",
        })

    compared = len(ids_in_data & dir_ids)
    for key, title in (("name", "название"), ("dev", "застройщик"), ("deadline", "срок сдачи")):
        if bad[key]:
            add(WARNING, f"{title.capitalize()} расходится у {len(bad[key])} ЖК из {compared}",
                "Примеры: " + _examples(bad[key]))
    if bad["multi"]:
        add(WARNING, f"У {len(bad['multi'])} ЖК в квартирах встречается больше одного названия",
            "id: " + _examples(bad["multi"]))
    if not any(bad[k] for k in ("name", "dev", "deadline")):
        add(OK, f"Название, застройщик и срок сдачи совпадают у всех {compared} ЖК.")

    if mp_compared:
        wrong = len(mp_lower) + len(mp_higher)
        if wrong:
            detail = []
            if mp_lower:
                detail.append("Дешевле справочника: " + _examples(mp_lower))
            if mp_higher:
                detail.append("Дороже справочника: " + _examples(mp_higher))
            add(WARNING, f"Минимальная цена расходится у {wrong} ЖК из {mp_compared}", " | ".join(detail))
        elif not mp_uncertain:
            add(OK, f"Минимальная цена ЖК совпала со справочником у всех {mp_compared} (допуск {tol * 100:g}%).")
        else:
            add(OK, f"Минимальная цена ЖК совпала со справочником у {mp_match} из {mp_compared} "
                    f"(допуск {tol * 100:g}%).")
        if mp_match and wrong:
            add(INFO, f"Минимальная цена совпала у {mp_match} из {mp_compared}.")
        if mp_uncertain:
            add(INFO, f"У {mp_uncertain} ЖК самая дешёвая квартира в данных дороже, чем в справочнике.",
                "Выборка неполна (порядок не по возрастанию цены), дешёвый лот мог в неё не попасть — "
                "расхождением не считается. При sort=price_asc или полном сборе такие ЖК считались бы расхождением.")
    elif compared:
        add(INFO, "Минимальную цену сверить не удалось: в справочнике или в квартирах нет цен.")

    # 5. покрытие справочника
    used = len(ids_in_data & dir_ids)
    cov = 100.0 * used / len(dir_ids)
    empty = len(dir_ids) - used
    if slice_mode:
        add(INFO, f"В срезе представлено {used} из {len(dir_ids)} ЖК справочника ({cov:.1f}%).",
            f"{empty} ЖК не попали в срез — это ожидаемо для max_records, не поломка.")
    elif cov < 50:
        add(CRITICAL, f"У {empty} из {len(dir_ids)} ЖК справочника нет ни одной квартиры (покрытие {cov:.1f}%).",
            "Для полного сбора это типичный след неполной выгрузки квартир.")
    elif cov < 90:
        add(WARNING, f"{empty} ЖК справочника без квартир (покрытие {cov:.1f}%).",
            "Может быть нормой (ЖК без лотов в продаже), но стоит выборочно проверить.")
    else:
        add(OK, f"Покрытие справочника {cov:.1f}% ({used} из {len(dir_ids)}).")

    return {
        "checks": checks, "rows": rows, "counts": _counts(checks),
        "summary": {"apartments": n_apts, "hc_in_data": len(ids_in_data),
                    "hc_in_directory": len(dir_ids), "coverage_pct": round(cov, 1)},
    }


def _counts(checks):
    return {lvl: sum(1 for c in checks if c["level"] == lvl) for lvl in (CRITICAL, WARNING, INFO, OK)}


# ──────────────────────────────────────────────────────────────
#  Вывод и файлы
# ──────────────────────────────────────────────────────────────
CSV_COLUMNS = ["hc_id", "name_directory", "name_apartments", "developer_directory",
               "developer_apartments", "deadline_directory", "deadline_apartments",
               "apartments_count", "min_price_directory", "min_price_apartments", "flags"]


def write_csv(rows, path) -> Path:
    path = Path(path)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return path


def print_report(res, csv_path=None, *, source_note="") -> None:
    s = res.get("summary", {})
    print("\n🔗 Перекрёстная проверка: ЖК из добытых квартир ↔ справочник ЖК")
    if s.get("hc_in_data") is not None and s.get("apartments"):
        print(f"   Квартир: {s['apartments']} | ЖК в квартирах: {s['hc_in_data']} | "
              f"ЖК в справочнике: {s['hc_in_directory']}{source_note}")
    for c in res["checks"]:
        print(f"   {ICON[c['level']]} {c['text']}")
        if c["detail"] and c["level"] != OK:
            print(f"      {c['detail']}")
    if csv_path:
        print(f"   📄 Разбор по каждому ЖК: {csv_path}")


# ──────────────────────────────────────────────────────────────
#  Подключение к сбору
# ──────────────────────────────────────────────────────────────
def _slice_flags(apt_spec):
    """(режим_среза, самая_дешёвая_квартира_ЖК_гарантированно_в_выборке)."""
    if apt_spec is None:
        return False, True
    # срез — если так и настроено (без дробления) или если сбор откатился на срез
    cap = bool(getattr(apt_spec, "collected_as_slice", False)
               or (getattr(apt_spec, "max_records", None) and not getattr(apt_spec, "split_param", None)))
    asc = "sort=price_asc" in (getattr(apt_spec, "url", "") or "")
    return cap, (not cap) or asc


def run_from_scraper(normalized, load_snapshot, apt_spec, out_dir, timestamp):
    """Вызывается из main(): использует то, что только что добыто; чего нет в
    этом запуске (например, справочника при --only apartments) — берёт из
    последнего сохранённого снимка. Ничего не запрашивает у API."""
    notes = []
    apartments = normalized.get("apartments")
    if not apartments:
        apartments = list(load_snapshot("apartments").values())
        if apartments:
            notes.append("квартиры — из прошлого сбора")
    directory = normalized.get("housing_complexes")
    if not directory:
        directory = list(load_snapshot("housing_complexes").values())
        if directory:
            notes.append("справочник ЖК — из прошлого сбора")

    slice_mode, min_complete = _slice_flags(apt_spec)
    res = reconcile(apartments or [], directory or [], slice_mode=slice_mode,
                    min_price_complete=min_complete)
    csv_path = write_csv(res["rows"], Path(out_dir) / f"crosscheck_hc_{timestamp}.csv") if res["rows"] else None
    print_report(res, csv_path, source_note=(" | " + "; ".join(notes)) if notes else "")
    return res


# ──────────────────────────────────────────────────────────────
#  Автономный запуск: по уже сохранённым данным, без сети и токена
# ──────────────────────────────────────────────────────────────
def load_rows(path):
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.values()) if isinstance(data, dict) else list(data)
    if path.suffix.lower() == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
    if path.suffix.lower() == ".parquet":
        import pandas as pd
        return pd.read_parquet(path).to_dict("records")
    raise ValueError(f"Неизвестный формат файла: {path.name}")


def main(argv=None) -> int:
    base = paths.ROOT
    p = argparse.ArgumentParser(description="Перекрёстная проверка ЖК ↔ квартиры по уже добытым данным.")
    p.add_argument("--apartments", help="файл с квартирами (.json снимок, .csv, .parquet); "
                                        "по умолчанию history/apartments.json, а если он пуст — последний reports/apartments_*")
    p.add_argument("--complexes", help="файл со справочником ЖК; по умолчанию history/housing_complexes.json")
    p.add_argument("--out", default=str(base / "reports"), help="куда положить CSV с разбором")
    p.add_argument("--slice", action="store_true", help="считать квартиры срезом (низкое покрытие — не поломка)")
    p.add_argument("--min-price-complete", action="store_true",
                   help="самая дешёвая квартира каждого ЖК точно в выборке (полный сбор или sort=price_asc)")
    a = p.parse_args(argv)

    # режим среза — из описания источника, если файл найден
    slice_mode, min_complete = a.slice, a.min_price_complete
    if not (a.slice or a.min_price_complete):
        try:
            sys.path.insert(0, str(base))
            from redcat.sources import registry as src
            src.load_from_dir(paths.SOURCES_DIR)
            slice_mode, min_complete = _slice_flags(src.get("apartments"))
        except Exception as e:  # noqa: BLE001 — описание источника необязательно
            logging.info("Описание источника apartments не прочитано: %s", e)

    apt_path = Path(a.apartments) if a.apartments else base / "history" / "apartments.json"
    dir_path = Path(a.complexes) if a.complexes else base / "history" / "housing_complexes.json"
    apartments = load_rows(apt_path) if apt_path.exists() else []
    if not apartments and not a.apartments:
        latest = sorted((base / "reports").glob("apartments_*.csv"))
        if latest:
            apt_path = latest[-1]
            apartments = load_rows(apt_path)
    directory = load_rows(dir_path) if dir_path.exists() else []
    print(f"Квартиры: {apt_path.name} ({len(apartments)}) | справочник: {dir_path.name} ({len(directory)})")
    print(f"Режим: {'срез' if slice_mode else 'полный сбор'}; "
          f"мин. цена {'сверяется в обе стороны' if min_complete else 'сверяется только «дешевле справочника»'}")

    res = reconcile(apartments, directory, slice_mode=slice_mode, min_price_complete=min_complete)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = write_csv(res["rows"], out / "crosscheck_hc.csv") if res["rows"] else None
    print_report(res, csv_path)
    return 1 if res["counts"][CRITICAL] else 0


if __name__ == "__main__":
    sys.exit(main())
