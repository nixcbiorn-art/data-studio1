"""
ПАТЧ 10 — фикс preflight_check_split (готовая версия)
=====================================================
Чинит:
  1. UnboundLocalError: первые строки функции обращались к переменным,
     которые определяются ниже (остаток от слияния).
  2. Чтение total/строк из ответа _fetch_json: работает и с unified-форматом
     {"items", "total", "raw"}, и с сырым JSON.
  3. Добавляет хелперы _total_of и _rows_of.

Сам определяет, принимает ли ваш _fetch_json аргумент spec=, и вызывает его
соответственно. Если _total_of уже добавлен вручную — не мешает.
Делает бэкап, проверяет компиляцию, при ошибке откатывает.

Запуск из папки проекта:  python apply_patch_10.py
"""
from __future__ import annotations

import py_compile
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

HERE = Path(__file__).resolve().parent
SCRAPER = HERE / "redcat_scraper.py"

START = "async def preflight_check_split(base_url, sample_values, token, spec):"
END = "async def _fetch_one_split("
DONE_MARK = "_total_of(base_data, spec)"

NEW_BLOCK = r'''def _total_of(data, spec):
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
            unfiltered_url, session, f"preflight:{spec.key}", 0__FETCH_KW__)
        unfiltered_total = _total_of(base_data, spec)

        # 2. Несколько значений фильтра. Два сбоя подряд — прекращаем.
        failed_in_row = 0
        for value in sample_values:
            url = (f"{base_url}{sep}{spec.split_param}={value}"
                   f"&{spec.page_size_param}={spec.page_size}"
                   f"&{spec.page_number_param}=1")
            data, status, body = await _fetch_json(
                url, session, f"preflight:{spec.key}", 1,
                max_attempts=1__FETCH_KW__)
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


'''


def compile_ok(p: Path):
    try:
        py_compile.compile(str(p), doraise=True, cfile=str(p) + ".pyc")
        return True, ""
    except py_compile.PyCompileError as e:
        return False, str(e)
    finally:
        Path(str(p) + ".pyc").unlink(missing_ok=True)


def apply() -> int:
    if not SCRAPER.exists():
        print(f"❌ Нет {SCRAPER.name}. Запускайте из папки проекта.")
        return 1
    text = SCRAPER.read_text(encoding="utf-8")

    if START not in text or END not in text:
        print("❌ Не найдены границы функции preflight_check_split / "
              "_fetch_one_split. Пришлите текущий redcat_scraper.py.")
        return 1
    start = text.index(START)
    end = text.index(END, start)
    if DONE_MARK in text[start:end]:
        print("ℹ️  Патч 10 уже применён.")
        return 0

    m = re.search(r"async def _fetch_json\(([^)]*)\)", text)
    if not m:
        print("❌ Не найден async def _fetch_json — файл неожиданной версии.")
        return 1
    has_spec = "spec" in m.group(1)
    fetch_kw = ", spec=spec" if has_spec else ""
    print(f"ℹ️  _fetch_json {'принимает' if has_spec else 'не принимает'} spec=")

    backup = SCRAPER.with_name(
        SCRAPER.name + f".bak_{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(SCRAPER, backup)
    print(f"💾 Бэкап: {backup.name}")

    block = NEW_BLOCK.replace("__FETCH_KW__", fetch_kw)
    SCRAPER.write_text(text[:start] + block + text[end:],
                       encoding="utf-8", newline="")
    ok, err = compile_ok(SCRAPER)
    if not ok:
        print(f"❌ Файл не компилируется: {err}\n   Откатываю.")
        shutil.copy2(backup, SCRAPER)
        return 1
    print("   ✓ preflight_check_split переписан")
    print("   ✓ добавлены _total_of и _rows_of")
    print("✅ Патч 10 применён.")
    return 0


def selftest() -> int:
    sys.path.insert(0, str(HERE))
    try:
        import redcat_scraper as rs
        import sources as src
    except Exception as e:  # noqa: BLE001
        print(f"❌ Импорт упал: {type(e).__name__}: {e}")
        return 1
    spec = src.SourceSpec(key="t", url="x", total_path=("meta", "total"),
                          data_path=("data",))
    bad = []
    unified = {"items": [1, 2], "total": 999, "raw": {"meta": {"total": 1}}}
    raw = {"data": [1, 2, 3], "meta": {"total": 123}}
    if rs._total_of(unified, spec) != 999:
        bad.append("_total_of unified")
    if rs._total_of(raw, spec) != 123:
        bad.append("_total_of raw")
    if rs._total_of(None, spec) is not None:
        bad.append("_total_of None")
    if rs._rows_of(unified, spec) != [1, 2]:
        bad.append("_rows_of unified")
    if rs._rows_of(raw, spec) != [1, 2, 3]:
        bad.append("_rows_of raw")
    if not hasattr(rs, "preflight_check_split"):
        bad.append("нет preflight_check_split")
    if bad:
        print("❌ Провалено: " + ", ".join(bad))
        return 1
    print("✅ Самопроверка пройдена.")
    return 0


if __name__ == "__main__":
    code = apply()
    if code == 0:
        code = selftest()
    sys.exit(code)
