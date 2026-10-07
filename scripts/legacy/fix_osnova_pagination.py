#!/usr/bin/env python3
"""
fix_osnova_pagination_v4.py — патчи 3-5 по реальному коду.
==============================================================================

Применяет все 5 правок. Патчи 1-2 идемпотентны (если уже применены —
пропустятся). Патчи 3-5 построены точно по тем фрагментам, что были
видны в вашем выводе:

  _fetch_one_split, строки 81-85 и 90-93
  fetch_pages, строки 80-86

Идемпотентно, бэкап .bak_<timestamp>.

Запуск:
    python fix_osnova_pagination_v4.py --dry-run
    python fix_osnova_pagination_v4.py
"""
from __future__ import annotations

from redcat.core import paths
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = paths.ROOT

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

TARGET = HERE / "redcat_scraper.py"


def _read(path: Path) -> tuple[str, str]:
    raw = path.read_bytes().decode("utf-8")
    style = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), style


def _write(path: Path, content: str, style: str) -> None:
    path.write_bytes(content.replace("\n", style).encode("utf-8"))


# ══════════════════════════════════════════════════════════════════════
#  ПАТЧ 1. fetched_raw = 0
# ══════════════════════════════════════════════════════════════════════
P1_OLD = (
    "    rows, page, incomplete, total, last_status = [], 1, False, None, None\n"
)
P1_NEW = (
    "    rows, page, incomplete, total, last_status = [], 1, False, None, None\n"
    "    # Счётчик СЫРЫХ записей (до препроцессора). Нужен для корректной\n"
    "    # остановки пагинации: osnova_flats выбрасывает нежилые, и если\n"
    "    # сравнивать с total по отфильтрованной длине, условие\n"
    "    # `collected >= total` не выполняется никогда — цикл крутится,\n"
    "    # пока сервер не начнёт отдавать пустые страницы. Именно так\n"
    "    # osnova_apartments дошёл до page=304.\n"
    "    fetched_raw = 0\n"
)

# ══════════════════════════════════════════════════════════════════════
#  ПАТЧ 2. Блок препроцессора + пустая страница + fetched_raw
# ══════════════════════════════════════════════════════════════════════
P2_OLD = (
    "            if spec.preprocess and spec.preprocess in src.PREPROCESSORS:\n"
    "                items = src.PREPROCESSORS[spec.preprocess](data.get(\"raw\"), value)\n"
    "            else:\n"
    "                items = data.get(\"items\") or []\n"
    "            if not isinstance(items, list):\n"
    "                logging.warning(\"[%s] стр.%d: в ответе нет списка записей.\",\n"
    "                                label, page)\n"
    "                break\n"
)
P2_NEW = (
    "            # Считаем СЫРЫЕ записи (до препроцессора). Если сравнивать\n"
    "            # с total по отфильтрованной длине, условие `>= total`\n"
    "            # не сработает никогда — цикл крутится до пустых страниц.\n"
    "            raw_items = data.get(\"items\") or []\n"
    "            if page > 1 and not raw_items:\n"
    "                break  # пустая страница — данные кончились\n"
    "            if page > 300:\n"
    "                logging.warning(\n"
    "                    \"[%s] больше 300 страниц — аварийная остановка, \"\n"
    "                    \"сбор помечен неполным\", label)\n"
    "                incomplete = True\n"
    "                break\n"
    "            fetched_raw += len(raw_items)\n"
    "            if spec.preprocess and spec.preprocess in src.PREPROCESSORS:\n"
    "                items = src.PREPROCESSORS[spec.preprocess](data.get(\"raw\"), value)\n"
    "            else:\n"
    "                items = raw_items\n"
    "            if not isinstance(items, list):\n"
    "                logging.warning(\"[%s] стр.%d: в ответе нет списка записей.\",\n"
    "                                label, page)\n"
    "                break\n"
)

# ══════════════════════════════════════════════════════════════════════
#  ПАТЧ 3. _fetch_one_split: ES_WINDOW и _next_url — по fetched_raw
# ══════════════════════════════════════════════════════════════════════
P3_OLD = (
    "            if len(rows) >= ES_WINDOW and total is not None and total > len(rows):\n"
    "                url = None\n"
    "            else:\n"
    "                url = _next_url(data.get(\"raw\"), spec, url, page,\n"
    "                                len(items), len(rows), total)\n"
)
P3_NEW = (
    "            # fetched_raw (а не len(rows)) — иначе условие останова\n"
    "            # никогда не сработает, если препроцессор отбрасывает часть\n"
    "            # записей. В _next_url передаём размер СЫРОЙ страницы и\n"
    "            # накопленное СЫРОЕ количество — пагинатор сравнивает с\n"
    "            # total, заявленным API, а не с числом отфильтрованных.\n"
    "            if fetched_raw >= ES_WINDOW and total is not None and total > fetched_raw:\n"
    "                url = None\n"
    "            else:\n"
    "                url = _next_url(data.get(\"raw\"), spec, url, page,\n"
    "                                len(raw_items), fetched_raw, total)\n"
)

# ══════════════════════════════════════════════════════════════════════
#  ПАТЧ 4. _fetch_one_split: финальная проверка недобора — по fetched_raw
# ══════════════════════════════════════════════════════════════════════
P4_OLD = (
    "    if total is not None and len(rows) < total:\n"
    "        logging.warning(\n"
    "            \"[%s] собрано %d из %d — похоже, лимит пагинации достигнут даже для \"\n"
    "            \"одного значения split.\", label, len(rows), total)\n"
    "        incomplete = True\n"
)
P4_NEW = (
    "    # Сравниваем СЫРОЕ количество с total: препроцессор мог законно\n"
    "    # выбросить часть записей (нежилые), и это не недобор.\n"
    "    if total is not None and fetched_raw < total:\n"
    "        logging.warning(\n"
    "            \"[%s] собрано %d из %d (сырых) — похоже, лимит пагинации \"\n"
    "            \"достигнут даже для одного значения split.\",\n"
    "            label, fetched_raw, total)\n"
    "        incomplete = True\n"
)

# ══════════════════════════════════════════════════════════════════════
#  ПАТЧ 5. fetch_pages: остановка на пустой странице
# ══════════════════════════════════════════════════════════════════════
P5_OLD = (
    "        items = data.get(\"items\") if isinstance(data, dict) else None\n"
    "        if not isinstance(items, list):\n"
    "            logging.warning(\"[%s] стр.%d: в ответе нет списка записей.\",\n"
    "                            label, page)\n"
    "            break\n"
    "\n"
    "        page_ids = {str(it.get(spec.id_field)) for it in items\n"
)
P5_NEW = (
    "        items = data.get(\"items\") if isinstance(data, dict) else None\n"
    "        if not isinstance(items, list):\n"
    "            logging.warning(\"[%s] стр.%d: в ответе нет списка записей.\",\n"
    "                            label, page)\n"
    "            break\n"
    "\n"
    "        # Пустая страница = данные кончились. Та же страховка, что в\n"
    "        # _fetch_one_split: у источника может быть заявлен total, но\n"
    "        # сервер молча отдаёт пустые страницы с 200-м кодом.\n"
    "        if page > 1 and not items:\n"
    "            break\n"
    "\n"
    "        page_ids = {str(it.get(spec.id_field)) for it in items\n"
)


PATCHES = [
    ("1. fetched_raw = 0", P1_OLD, P1_NEW, "fetched_raw = 0"),
    ("2. блок препроцессора + raw_items", P2_OLD, P2_NEW, "raw_items = data.get"),
    ("3. ES_WINDOW и _next_url по fetched_raw", P3_OLD, P3_NEW,
     "fetched_raw >= ES_WINDOW"),
    ("4. финальная проверка недобора по fetched_raw", P4_OLD, P4_NEW,
     "fetched_raw < total"),
    ("5. fetch_pages: стоп на пустой странице", P5_OLD, P5_NEW,
     "if page > 1 and not items:"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("=" * 78)
    print("  FIX OSNOVA PAGINATION v4")
    print("=" * 78)
    print(f"  Файл: {TARGET}")

    if not TARGET.exists():
        print("❌ Не найден")
        return 1

    content, style = _read(TARGET)

    applied = already = notfound = 0
    for label, old, new, marker in PATCHES:
        if marker in content and old not in content:
            print(f"  ↻  {label} — уже применено")
            already += 1
            continue
        if old not in content:
            print(f"  ⚠️  {label} — якорь не найден")
            notfound += 1
            continue
        if content.count(old) > 1:
            print(f"  ⚠️  {label} — якорь встречается {content.count(old)} раз")
            notfound += 1
            continue
        if args.dry_run:
            print(f"  ·  {label}")
        else:
            content = content.replace(old, new, 1)
            print(f"  ✅ {label}")
        applied += 1

    print()
    print("=" * 78)
    print(f"  применено: {applied}   уже было: {already}   "
          f"не найдено: {notfound}")
    print("=" * 78)

    if args.dry_run:
        print("  --dry-run: файл не менялся.")
        return 0 if notfound == 0 else 2

    if applied == 0:
        print("  Нечего менять.")
        return 0 if notfound == 0 else 2

    backup = TARGET.with_name(
        TARGET.name + f".bak_{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(TARGET, backup)
    _write(TARGET, content, style)

    print(f"  Бэкап: {backup.name}")
    print()
    print("Дальше:")
    print("  1. Синтаксис:  python -m py_compile redcat_scraper.py")
    print("  2. Импорт:     python -c \"import redcat_scraper\"")
    print("  3. Перезапуск: python redcat_scraper.py --only osnova_apartments")
    print()
    print("Ожидаемое поведение:")
    print("  • В логе osnova_apartments больше не будет page=304, 305, …")
    print("  • Первый ответ даёт pagination.last_page (для проекта 12 — 8).")
    print("  • После последней страницы fetched_raw >= total → url=None → стоп.")
    print("  • Даже если API отдаст пустую страницу раньше — сработает")
    print("    `if page > 1 and not raw_items: break`.")
    return 0 if notfound == 0 else 2


if __name__ == "__main__":
    sys.exit(main())