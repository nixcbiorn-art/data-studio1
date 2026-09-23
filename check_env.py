"""
Диагностика файла .env: показывает, какие строки не разобраны и почему.
Значения НЕ печатаются (токен — секрет): только длины, номера строк и тип проблемы,
так что вывод можно спокойно копировать и присылать.

Использование:
    python check_env.py            # .env рядом со скриптом
    python check_env.py путь\\к\\.env
"""
import base64
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / ".env"
print(f"Файл: {path}")
if not path.exists():
    print("❌ Файла нет. Скопируйте .env.example в .env и впишите токен.")
    sys.exit(1)

raw = path.read_bytes()
print(f"Размер: {len(raw)} байт")
if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw:
    print("❌ Файл сохранён в UTF-16 (так бывает при  echo ... > .env  в PowerShell).")
    print("   Python-dotenv такое не читает. Пересохраните как UTF-8 или задайте токен")
    print("   командой:  python set_token.py")
    sys.exit(1)
if raw.startswith(b"\xef\xbb\xbf"):
    print("⚠️ Есть BOM (UTF-8 with BOM). Первая строка может не распознаться — пересохраните как UTF-8 без BOM.")
print("Переносы строк:", "CRLF (Windows)" if b"\r\n" in raw else "LF")

text = raw.decode("utf-8", errors="replace")
try:
    from dotenv import dotenv_values
except ImportError:
    print("❌ Не установлен python-dotenv:  pip install python-dotenv")
    sys.exit(1)

bad = []


class Catch(logging.Handler):
    def emit(self, record):
        m = re.search(r"line (\d+)", record.getMessage())
        if m:
            bad.append(int(m.group(1)))


lg = logging.getLogger("dotenv.main")
lg.addHandler(Catch())
lg.propagate = False
values = dotenv_values(stream=__import__("io").StringIO(text))

print("\nПострочный разбор (значения скрыты):")
for n, line in enumerate(text.splitlines(), 1):
    body = line.strip()
    if not body:
        kind = "пустая строка"
    elif body.startswith("#"):
        kind = "комментарий"
    elif n in bad:
        why = []
        if body.startswith("="):
            why.append("начинается с «=» — нет имени переменной")
        elif "=" not in body:
            why.append("нет «=» и внутри есть пробелы — это текст, а не КЛЮЧ=значение")
        elif re.search(r"\s", body.split("=", 1)[0].strip()):
            why.append("пробел в имени переменной до «=»")
        if re.search(r"[^\x00-\x7f]", body):
            why.append("есть не-ASCII символы (кириллица, неразрывный пробел и т.п.)")
        if body.count('"') % 2 or body.count("'") % 2:
            why.append("незакрытая кавычка")
        kind = "❌ НЕ РАЗОБРАНА: " + ("; ".join(why) or "неверный формат")
    elif "=" in body:
        key, val = body.split("=", 1)
        kind = f"{key.strip()} = <{len(val.strip())} симв.>"
    else:
        kind = "голое слово без «=» (будет считаться переменной без значения)"
    print(f"  {n:>3}: len={len(line):<5} {kind}")

tok = (values.get("REDCAT_TOKEN") or "").strip()
print()
if not tok:
    print("❌ REDCAT_TOKEN не найден или пуст.")
else:
    parts = tok.split(".")
    print(f"REDCAT_TOKEN: {len(tok)} символов, частей через точку: {len(parts)}")
    if re.search(r"\s", tok):
        print("⚠️ В токене есть пробелы/переносы — уберите их (проще: python set_token.py).")
    if len(parts) != 3:
        print("❌ Это не JWT (нужно 3 части). Вероятно, токен обрезан при вставке.")
    else:
        try:
            p = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(p))
            exp = datetime.fromtimestamp(claims["exp"])
            state = "ИСТЁК" if exp < datetime.now() else "действителен"
            print(f"   срок действия: {exp:%d.%m.%Y %H:%M} — {state}")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ Не удалось прочитать срок из токена: {type(e).__name__}")

if bad:
    print(f"\nИтого неразобранных строк: {len(set(bad))} → {sorted(set(bad))}")
    print("Исправление: удалите эти строки или поставьте в начале «# ».")
else:
    print("\n✅ Все строки разобраны.")
