"""
Проверка патча asymmetric для А101.
=====================================
Замени содержимое apply_patch_tg_bot.py этим, запусти.

Что проверяет:
  1. Применился ли флаг asymmetric в спеке a101_apartments.
  2. Применился ли патч в webapp.py.
  3. Как отчёт теперь классифицирует А101 — сравнивает
     то, что получится с флагом и без него.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def find_spec_file(key: str):
    for folder in (HERE / "sources", HERE / "sources_external"):
        if not folder.exists():
            continue
        for p in sorted(folder.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            items = data if isinstance(data, list) else [data]
            for it in items:
                if isinstance(it, dict) and it.get("key") == key:
                    return p, it
    return None, None


def main() -> int:
    print("=" * 78)
    print("  Проверка патча asymmetric")
    print("=" * 78)

    # ── 1. Спека А101 ──
    print("\n1. Спека a101_apartments:")
    path, item = find_spec_file("a101_apartments")
    if not path:
        print("   ❌ не нашёл файл описания")
        return 1
    print(f"   Файл: {path.name}")
    cc = (item or {}).get("cross_check") or {}
    asym = cc.get("asymmetric")
    print(f"   cross_check.asymmetric = {asym!r}")
    if asym == "source_higher":
        print("   ✅ флаг на месте")
    else:
        print("   ❌ флаг НЕ на месте — патч не сработал")

    # ── 2. webapp.py ──
    print("\n2. webapp.py:")
    webapp = HERE / "webapp.py"
    if not webapp.exists():
        print("   ❌ файл не найден")
        return 1
    text = webapp.read_text(encoding="utf-8", errors="replace")
    has_asym = "_asym_filter" in text
    has_source_higher = 'source_higher' in text
    print(f"   _asym_filter в коде: {'✅' if has_asym else '❌'}")
    print(f"   source_higher в коде: {'✅' if has_source_higher else '❌'}")
    if has_asym and has_source_higher:
        print("   ✅ патч webapp применён")
    else:
        print("   ❌ патч webapp НЕ применён")

    # ── 3. Что отчёт выдаёт сейчас ──
    print("\n3. Что сейчас в отчёте по А101:")
    try:
        import webapp as w

        # Перезагрузим модуль на случай кэша.
        import importlib
        importlib.reload(w)

        rep = w._cross_check_report("a101_apartments")
        s = rep.get("summary") or {}
        print(f"   matched  = {s.get('matched')}")
        print(f"   critical = {s.get('critical')}")
        print(f"   warn     = {s.get('warn')}")
        print(f"   ok       = {s.get('ok')}")
        print()
        # Покажем warn/crit конкретно.
        for cls in ("critical", "warn"):
            items = rep.get("items_by_class", {}).get(cls) or []
            for it in items:
                print(f"   [{cls}] {it.get('display')!r}")
                for mk, mm in (it.get("metrics") or {}).items():
                    print(f"      {mk}: source={mm.get('left')} "
                          f"rc={mm.get('right')} "
                          f"Δ={mm.get('diff_pct')}%")
    except Exception as e:
        print(f"   ❌ Ошибка при вызове отчёта: {type(e).__name__}: {e}")
        return 1

    print()
    print("=" * 78)
    print("  Что ожидать:")
    print("  • critical = 0")
    print("  • warn = 0 (все расхождения +Δ% объясняются второй скидкой)")
    print("  • Если warn > 0 и все они с ОТРИЦАТЕЛЬНЫМ Δ% — это")
    print("    реальные проблемы, которые надо смотреть руками")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())