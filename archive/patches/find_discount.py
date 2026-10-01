"""
fix_lsr_filter.py — добавить в cross_check фильтр "только квартиры".
====================================================================
Сейчас в cross_check у внешнего источника ЛСР фильтр справа:
    developer_name contains ЛСР

Он находит и квартиры, и коммерческую недвижимость, и машино-места.
Из-за этого средние цены по ЖК не совпадают с внешним источником,
где только квартиры.

Патч добавляет в cross_check ещё одно условие:
    object_type = Квартира

Скрипт сам ищет файл в sources_external/, у которого в spec есть
cross_check, и обновляет filter_right. Работает, даже если filter_right
сейчас dict, а не list.
"""
from __future__ import annotations
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXT_DIR = HERE / "sources_external"

if not EXT_DIR.exists():
    print(f"❌ Нет папки {EXT_DIR}")
    sys.exit(1)


def patch_file(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  ⚠️  {path.name}: не парсится: {e}")
        return False

    items = data if isinstance(data, list) else [data]
    changed = False

    for it in items:
        if not isinstance(it, dict):
            continue
        cc = it.get("cross_check")
        if not cc:
            continue

        current = cc.get("filter_right")
        # приводим к списку
        if current is None:
            filters = []
        elif isinstance(current, list):
            filters = list(current)
        else:
            filters = [current]

        # уже есть фильтр по object_type?
        has_ot = any(
            isinstance(f, dict) and "object_type" in str(f.get("field", ""))
            for f in filters
        )
        if has_ot:
            print(f"  · {path.name} ({it.get('key')}): "
                  f"фильтр по object_type уже есть")
            continue

        # добавляем
        filters.append({
            "field": "object_type",
            "op": "eq",
            "value": "Квартира",
        })
        cc["filter_right"] = filters
        changed = True
        print(f"  ✓ {path.name} ({it.get('key')}): добавлен фильтр "
              f"object_type = 'Квартира'")

    if not changed:
        return False

    # бэкап
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(path.name + f".bak_{stamp}")
    shutil.copy2(path, backup)
    print(f"    💾 {backup.name}")

    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def main():
    print("=" * 68)
    print("  Поиск spec'ов с cross_check в sources_external/")
    print("=" * 68)

    any_changed = False
    for path in sorted(EXT_DIR.glob("*.json")):
        if patch_file(path):
            any_changed = True

    print()
    if any_changed:
        print("✅ Готово. Перезапустите studio.py:")
        print("     Ctrl+C в окне приложения, потом снова: python studio.py")
        print("   И Ctrl+F5 в браузере.")
        print()
        print("   Затем: вкладка «Внешние» → ⇄ Сверить с Redcat.")
        print("   Средние цены должны сойтись — теперь сверяются только")
        print("   квартиры, без машиномест и коммерции.")
    else:
        print("ℹ️  Изменений не потребовалось (уже настроено).")


if __name__ == "__main__":
    main()