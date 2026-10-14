"""scrape_token — разбор и получение токена доступа."""
from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime
from redcat.collection.scrape_env import _ENV_BAD_LINES, _ENV_FILE_USED, _SHADOWED_OS_TOKEN, _TOKEN_SOURCE, clean_token


# ──────────────────────────────────────────────────────────────
#  ТОКЕН
# ──────────────────────────────────────────────────────────────
def decode_jwt_payload(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def resolve_token(cli_token):
    oneshot = os.environ.get("REDCAT_TOKEN_ONESHOT", "").strip()
    token = clean_token(cli_token or oneshot or os.environ.get("REDCAT_TOKEN", ""))
    if cli_token:
        source = "--token"
    elif oneshot:
        source = "разовый токен (веб-форма)"
    else:
        source = _TOKEN_SOURCE or "REDCAT_TOKEN"
    if not token:
        print("❌ ОШИБКА: токен не передан.")
        print('   python -m redcat.collection.redcat_scraper --token "eyJ..."  либо REDCAT_TOKEN в .env')
        sys.exit(1)

    where = f"{source}: {_ENV_FILE_USED}" if source == ".env" and _ENV_FILE_USED else source
    print(f"🔑 Источник токена — {where}")
    if _ENV_BAD_LINES:
        lines_txt = ", ".join(str(n) for n in sorted(set(_ENV_BAD_LINES)))
        print(f"⚠️ В .env не разобраны строки: {lines_txt} — они игнорируются.")
        print("   Что именно не так: python check_env.py")
    if token.count(".") != 2:
        print("⚠️ Токен не похож на JWT (нужны три части через точку).")
    if _SHADOWED_OS_TOKEN and source == ".env":
        print("⚠️ В окружении ОС задан другой REDCAT_TOKEN — он игнорируется.")

    claims = decode_jwt_payload(token)
    if claims and "exp" in claims:
        try:
            exp = datetime.fromtimestamp(claims["exp"])
            if exp < datetime.now():
                print(f"❌ Срок действия токена истёк {exp:%d.%m.%Y %H:%M}.")
                sys.exit(1)
            left = exp - datetime.now()
            print(f"🔑 Токен действителен до {exp:%d.%m.%Y %H:%M} (осталось {left}).")
            if left.total_seconds() < 24 * 3600:
                print("⚠️ Токен скоро истечёт — обновите его в .env.")
        except (OSError, OverflowError, ValueError):
            pass
    return token
