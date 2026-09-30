"""
set_telegram.py — интерактивная настройка Telegram-бота.
==========================================================
Спрашивает токен и chat_id, проверяет их через Telegram API и
записывает в .env. Если токен невалидный — даёт понятную ошибку
и ничего не пишет.

Запуск:
    python set_telegram.py
    python set_telegram.py --token "123:ABC" --chat 123456789   # без вопросов
    python set_telegram.py --test                                # проверить, что уже в .env
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import requests

# Чтобы вывод не падал с UnicodeEncodeError на русской Windows.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"
API = "https://api.telegram.org"
TIMEOUT = 20


# ──────────────────────────────────────────────────────────────
#  .env: чтение и запись
# ──────────────────────────────────────────────────────────────
def _read_env_lines() -> list[str]:
    if not ENV.exists():
        return []
    return ENV.read_text(encoding="utf-8", errors="replace").splitlines()


def _upsert(lines: list[str], key: str, value: str) -> list[str]:
    """Заменяет строку KEY=... или добавляет её в конец файла."""
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    out = []
    found = False
    for line in lines:
        if pat.match(line):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key}={value}")
    return out


def _write_env(lines: list[str]) -> None:
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _existing(key: str) -> str:
    for line in _read_env_lines():
        s = line.strip()
        if s.startswith(f"{key}="):
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ──────────────────────────────────────────────────────────────
#  Проверки
# ──────────────────────────────────────────────────────────────
def _clean_token(raw: str) -> str:
    t = re.sub(r"\s+", "", raw or "").strip("\"'")
    if t.lower().startswith("bearer"):
        t = t[6:]
    return t.strip("\"'")


def _token_looks_valid(token: str) -> tuple[bool, str]:
    """Проверка формата: <цифры>:<alnum/_-/...>. Не ходит в сеть."""
    if not token:
        return False, "пусто"
    if ":" not in token:
        return False, "нет двоеточия между ID бота и секретом"
    left, right = token.split(":", 1)
    if not left.isdigit():
        return False, "левая часть до «:» должна быть числом (ID бота)"
    if len(right) < 20:
        return False, "правая часть слишком короткая"
    return True, ""


def _api_call(method: str, token: str, **params) -> dict | None:
    try:
        r = requests.get(f"{API}/bot{token}/{method}",
                         params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"  ⚠️  сеть недоступна: {type(e).__name__}: {e}")
        return None
    try:
        data = r.json()
    except ValueError:
        print(f"  ⚠️  ответ не JSON (HTTP {r.status_code})")
        return None
    if not data.get("ok"):
        print(f"  ⚠️  Telegram: {data.get('description', 'ошибка')}")
        return None
    return data


def _check_token_online(token: str) -> tuple[bool, str, dict | None]:
    """getMe. Возвращает (ok, message, me)."""
    data = _api_call("getMe", token)
    if not data:
        return False, "Telegram отклонил токен (или нет сети)", None
    me = data.get("result") or {}
    name = me.get("username") or me.get("first_name") or "?"
    return True, f"бот @{name}", me


def _check_chat_online(token: str, chat_id: str) -> tuple[bool, str]:
    """Проверяет чат через getChat. Для этого бот должен уже «видеть» чат —
    пользователь должен был написать боту хоть одно сообщение."""
    data = _api_call("getChat", token, chat_id=chat_id)
    if not data:
        return False, ("Telegram не знает такой чат для этого бота. "
                       "Напишите боту /start в личку (или добавьте "
                       "бота в группу и напишите туда), потом повторите.")
    r = data.get("result") or {}
    title = r.get("title") or r.get("username") or r.get("first_name") or chat_id
    return True, f"чат найден: {title}"


# ──────────────────────────────────────────────────────────────
#  Интерактивный опрос
# ──────────────────────────────────────────────────────────────
def _ask(prompt: str, current: str = "") -> str:
    suffix = ""
    if current:
        masked = current if len(current) <= 12 else current[:6] + "…" + current[-4:]
        suffix = f" [{masked}]"
    try:
        s = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return s or current


def ask_and_write(token_arg: str = None, chat_arg: str = None) -> int:
    print("=" * 66)
    print("  Настройка Telegram-бота (tg_bot.py)")
    print("=" * 66)
    print()
    print("Где взять:")
    print("  • Токен — у @BotFather в Telegram: /newbot или /mybots → API Token")
    print("  • chat_id — напишите своему боту /start, потом запустите")
    print("    этот скрипт с ключом --test — увидите подсказку.")
    print()

    cur_token = _existing("TELEGRAM_BOT_TOKEN")
    cur_chat  = _existing("TELEGRAM_CHAT_ID")

    # Токен.
    token = token_arg if token_arg else _ask("Токен бота", cur_token)
    token = _clean_token(token)
    if not token:
        print("❌ Токен пустой — ничего не записано.")
        return 1

    ok, reason = _token_looks_valid(token)
    if not ok:
        print(f"❌ Формат токена не похож на настоящий: {reason}.")
        print("   Ожидается примерно: 123456789:AAE...xyz")
        return 1

    print("  · проверяю токен в Telegram…")
    ok, info, me = _check_token_online(token)
    if not ok:
        print(f"❌ {info}")
        return 1
    print(f"  ✅ {info}")

    # Chat ID.
    chat_id = chat_arg if chat_arg else _ask("Chat ID получателя", cur_chat)
    chat_id = chat_id.strip().strip("\"'")
    if not chat_id:
        # Попробуем подсказать из getUpdates.
        print("  · chat_id не указан, смотрю getUpdates…")
        upd = _api_call("getUpdates", token, limit=5)
        found = []
        if upd:
            for u in upd.get("result") or []:
                msg = u.get("message") or u.get("edited_message") or {}
                ch = msg.get("chat") or {}
                if ch.get("id"):
                    found.append((str(ch["id"]),
                                  ch.get("username") or ch.get("title")
                                  or ch.get("first_name") or "?"))
        if found:
            print("  Нашёл такие чаты (кто писал боту):")
            for cid, title in found[:5]:
                print(f"    {cid}  ({title})")
            chat_id = _ask("Укажите chat_id из списка (или введите вручную)")
        if not chat_id:
            print("❌ Без chat_id бот не сможет отправлять сообщения.")
            print("   Напишите боту /start и запустите скрипт снова.")
            return 1

    print("  · проверяю chat_id…")
    ok, info = _check_chat_online(token, chat_id)
    if not ok:
        print(f"❌ {info}")
        # Разрешим сохранить всё равно — иногда getChat капризничает,
        # а sendMessage работает.
        ans = input("  Сохранить как есть, чтобы попробовать отправку? [y/N]: ")
        if ans.strip().lower() not in ("y", "yes", "д", "да"):
            return 1
    else:
        print(f"  ✅ {info}")

    # Дополнительные чаты — необязательно.
    extra_cur = _existing("TELEGRAM_EXTRA_CHATS")
    extra = _ask("Дополнительные chat_id через запятую (необязательно)",
                 extra_cur)

    # Запись.
    lines = _read_env_lines()
    lines = _upsert(lines, "TELEGRAM_BOT_TOKEN", token)
    lines = _upsert(lines, "TELEGRAM_CHAT_ID", chat_id)
    if extra:
        lines = _upsert(lines, "TELEGRAM_EXTRA_CHATS", extra)
    _write_env(lines)

    print()
    print(f"✅ Записано в {ENV.name}:")
    print(f"   TELEGRAM_BOT_TOKEN=…{token[-6:]}")
    print(f"   TELEGRAM_CHAT_ID={chat_id}")
    if extra:
        print(f"   TELEGRAM_EXTRA_CHATS={extra}")

    # Тестовая отправка — сразу видно, всё ли в порядке.
    ans = input("\nОтправить тестовое сообщение боту сейчас? [Y/n]: ")
    if ans.strip().lower() in ("", "y", "yes", "д", "да"):
        try:
            r = requests.post(
                f"{API}/bot{token}/sendMessage",
                data={"chat_id": chat_id,
                      "text": "✅ Бот настроен и готов к работе.\n"
                              "Попробуйте /help."},
                timeout=TIMEOUT)
            ok = r.json().get("ok")
            if ok:
                print("✅ Сообщение доставлено.")
            else:
                print(f"❌ Telegram отказал: {r.json().get('description')}")
        except requests.RequestException as e:
            print(f"❌ Не удалось отправить: {type(e).__name__}: {e}")

    print()
    print("Дальше:")
    print("  python tg_bot.py            # long polling")
    print("  python tg_bot.py --watch    # ещё и слать новые отчёты")
    return 0


# ──────────────────────────────────────────────────────────────
#  --test: показать текущий статус, ничего не менять
# ──────────────────────────────────────────────────────────────
def test_only() -> int:
    token = _existing("TELEGRAM_BOT_TOKEN")
    chat_id = _existing("TELEGRAM_CHAT_ID")
    print(f"Файл: {ENV}")
    print(f"Токен: {'задан' if token else 'НЕ задан'}"
          + (f" (…{token[-6:]})" if token else ""))
    print(f"Chat ID: {chat_id or 'НЕ задан'}")
    if not token:
        return 1

    ok, info, me = _check_token_online(token)
    if not ok:
        print(f"❌ {info}")
        return 1
    print(f"✅ Токен валиден: {info}")

    upd = _api_call("getUpdates", token, limit=10)
    if upd:
        seen = []
        for u in upd.get("result") or []:
            msg = u.get("message") or u.get("edited_message") or {}
            ch = msg.get("chat") or {}
            if ch.get("id"):
                seen.append((str(ch["id"]),
                             ch.get("username") or ch.get("title")
                             or ch.get("first_name") or "?"))
        if seen:
            print("Чаты, которые бот уже видел (могут быть chat_id):")
            for cid, title in seen[:10]:
                mark = " ← в .env" if cid == chat_id else ""
                print(f"  {cid}  ({title}){mark}")
        else:
            print("getUpdates пуст. Напишите боту /start, потом повторите --test.")

    if chat_id:
        ok, info = _check_chat_online(token, chat_id)
        print(("✅ " if ok else "❌ ") + info)
    return 0


# ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Интерактивная настройка Telegram-бота (пишет в .env).")
    ap.add_argument("--token", default=None,
                    help="токен бота (пропустит вопрос про токен)")
    ap.add_argument("--chat", default=None,
                    help="chat_id (пропустит вопрос про chat_id)")
    ap.add_argument("--test", action="store_true",
                    help="только проверить текущие настройки, ничего не менять")
    args = ap.parse_args()

    if args.test:
        return test_only()
    return ask_and_write(token_arg=args.token, chat_arg=args.chat)


if __name__ == "__main__":
    sys.exit(main())