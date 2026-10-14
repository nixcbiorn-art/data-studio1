"""web_telegram — настройки Telegram и токена доступа (.env)."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from redcat.web.web_config import ENV_FILE


def write_telegram_env(bot_token, chat_id):
    bot_token = re.sub(r"\s+", "", bot_token or "").strip("\"'")
    chat_id = str(chat_id or "").strip().strip("\"'")
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    ft = fc = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("TELEGRAM_BOT_TOKEN="):
            lines[i] = f"TELEGRAM_BOT_TOKEN={bot_token}"; ft = True
        elif s.startswith("TELEGRAM_CHAT_ID="):
            lines[i] = f"TELEGRAM_CHAT_ID={chat_id}"; fc = True
    if not ft: lines.append(f"TELEGRAM_BOT_TOKEN={bot_token}")
    if not fc and chat_id: lines.append(f"TELEGRAM_CHAT_ID={chat_id}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def telegram_status():
    out = {"token_set": False, "token_preview": "", "chat_id": "",
           "env_exists": ENV_FILE.exists(), "env_file": str(ENV_FILE)}
    if not ENV_FILE.exists(): return out
    try:
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("TELEGRAM_BOT_TOKEN="):
                v = s.split("=", 1)[1].strip().strip("\"'")
                if v and "вставьте" not in v.lower():
                    out["token_set"] = True
                    out["token_preview"] = ("..." + v[-4:]) if len(v) > 4 else "..."
            elif s.startswith("TELEGRAM_CHAT_ID="):
                out["chat_id"] = s.split("=", 1)[1].strip().strip("\"'")
    except OSError: pass
    return out


def telegram_probe(bot_token, chat_id):
    import re as _re
    out = {"ok": True, "token_ok": False, "token_error": None,
           "bot_username": None, "chat_ok": False, "chat_error": None,
           "chat_title": None, "updates": []}
    if not bot_token:
        out["ok"] = False; out["token_error"] = "Токен не задан"; return out
    if not _re.match(r"^\d+:[A-Za-z0-9_\-]{20,}$", bot_token):
        out["token_error"] = "Формат не похож на токен бота"
    try:
        r = requests.get(f"https://api.telegram.org/bot{bot_token}/getMe", timeout=15)
        data = r.json()
    except Exception as e:
        out["ok"] = False; out["token_error"] = f"{type(e).__name__}: {e}"; return out
    if not data.get("ok"):
        out["ok"] = False
        out["token_error"] = data.get("description") or "Telegram отклонил"
        return out
    me = data.get("result") or {}
    out["token_ok"] = True
    out["bot_username"] = me.get("username") or me.get("first_name") or "?"
    try:
        r = requests.get(f"https://api.telegram.org/bot{bot_token}/getUpdates",
                         params={"limit": 30}, timeout=15)
        upd = r.json()
        if upd.get("ok"):
            seen = {}
            for u in upd.get("result") or []:
                msg = u.get("message") or u.get("edited_message") or {}
                ch = msg.get("chat") or {}; cid = ch.get("id")
                if cid is None or str(cid) in seen: continue
                seen[str(cid)] = (ch.get("username") or ch.get("title")
                                  or ch.get("first_name") or str(cid))
            out["updates"] = [{"chat_id": k, "title": v} for k, v in seen.items()]
    except Exception: pass
    if chat_id:
        try:
            r = requests.get(f"https://api.telegram.org/bot{bot_token}/getChat",
                             params={"chat_id": chat_id}, timeout=15)
            data = r.json()
            if data.get("ok"):
                out["chat_ok"] = True
                res = data.get("result") or {}
                out["chat_title"] = (res.get("title") or res.get("username")
                                     or res.get("first_name") or chat_id)
            else:
                out["chat_error"] = data.get("description") or "Не знает чат"
        except Exception as e:
            out["chat_error"] = f"{type(e).__name__}: {e}"
    return out


# ──────────────────────────────────────────────────────────────
#  ТОКЕН
# ──────────────────────────────────────────────────────────────
def read_env_token() -> str:
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("REDCAT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return os.environ.get("REDCAT_TOKEN", "")


def _clean_token(raw: str) -> str:
    t = re.sub(r"\s+", "", raw or "").strip("\"'")
    if t.lower().startswith("bearer"):
        t = t[6:]
    return t.strip("\"'")


def write_env_token(token: str) -> None:
    """LOCAL-ONLY: пишет токен в .env на вашем диске."""
    token = _clean_token(token)
    lines, found = [], False
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("REDCAT_TOKEN="):
                lines.append(f"REDCAT_TOKEN={token}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"REDCAT_TOKEN={token}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def token_status() -> dict:
    import base64
    token = read_env_token()
    if not token or "вставьте" in token:
        return {"state": "missing", "text": "Токен не задан"}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = datetime.fromtimestamp(claims["exp"])
    except Exception:
        return {"state": "unknown", "text": "Токен задан (срок неизвестен)"}
    left = exp - datetime.now()
    if left.total_seconds() <= 0:
        return {"state": "expired",
                "text": f"Истёк {exp:%d.%m.%Y %H:%M}",
                "expires_at": exp.isoformat()}
    hours = left.total_seconds() / 3600
    return {"state": "soon" if hours < 24 else "ok",
            "text": f"Действует до {exp:%d.%m.%Y %H:%M} "
                    f"(осталось {int(hours)} ч)",
            "expires_at": exp.isoformat()}
