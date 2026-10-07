"""
Управление пользователями Telegram-бота.
==========================================
Хранит {chat_id: {role, name, added_at}} в bot_users.json.
Роли: admin, viewer. Список pending — те, кто писал, но не добавлен.

Ничего не знает про Redcat — просто хранилище.
"""
from __future__ import annotations

from redcat.core import paths
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

HERE = paths.ROOT
USERS_FILE = HERE / "bot_users.json"
PENDING_FILE = HERE / "bot_pending.json"

# Роли и разрешённые команды.
ROLES = ("admin", "viewer")

# Команды, доступные только admin.
ADMIN_ONLY = frozenset({
    "/run", "/run_status", "/settings", "/report",
    "/log", "/html",
    "/users", "/add_user", "/remove_user", "/set_role", "/pending",
    "/invite", "/invites", "/revoke",
    "/hidekbd", "/showkbd",
})

# Команды, доступные всем ролям (в т.ч. viewer).
VIEWER_COMMANDS = frozenset({
    "/start", "/help", "/ping", "/status", "/top",
    "/find", "/jc", "/filter", "/fill",
    "/files", "/diff",
})

_lock = threading.Lock()


def _read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError as e:
        logging.warning("bot_users: не записал %s: %s", path.name, e)


def _load() -> dict:
    data = _read_json(USERS_FILE, {"users": {}})
    if "users" not in data or not isinstance(data["users"], dict):
        data["users"] = {}
    return data


def _save(data: dict) -> None:
    _write_json(USERS_FILE, data)


def ensure_admin(chat_id: str, name: str = "") -> bool:
    """Гарантирует, что chat_id есть в списке как admin.

    Используется при старте бота для TELEGRAM_CHAT_ID — чтобы бот
    не оказался без админа после переезда или ручной правки файла.
    Возвращает True, если пользователь был добавлен.
    """
    chat_id = str(chat_id)
    if not chat_id:
        return False
    with _lock:
        data = _load()
        if chat_id in data["users"]:
            return False
        data["users"][chat_id] = {
            "role": "admin",
            "name": name or "",
            "added_at": datetime.now().isoformat(timespec="seconds"),
        }
        _save(data)
    return True


def get_role(chat_id: str) -> str | None:
    """Вернёт 'admin' / 'viewer' или None, если не в списке."""
    chat_id = str(chat_id)
    with _lock:
        data = _load()
        u = data["users"].get(chat_id)
    return u.get("role") if isinstance(u, dict) else None


def get_name(chat_id: str) -> str:
    chat_id = str(chat_id)
    with _lock:
        data = _load()
        u = data["users"].get(chat_id)
    return (u or {}).get("name", "") or ""


def is_admin(chat_id: str) -> bool:
    return get_role(chat_id) == "admin"


def add_user(chat_id: str, role: str = "viewer",
             name: str = "") -> tuple[bool, str]:
    """Добавить или обновить. Возвращает (ok, msg)."""
    chat_id = str(chat_id)
    if role not in ROLES:
        return False, f"неизвестная роль {role!r}. Доступны: {', '.join(ROLES)}"
    if not chat_id.lstrip("-").isdigit():
        return False, f"это не похоже на chat_id: {chat_id!r}"
    with _lock:
        data = _load()
        existed = chat_id in data["users"]
        data["users"][chat_id] = {
            "role": role,
            "name": name or (data["users"].get(chat_id, {}).get("name", "")),
            "added_at": datetime.now().isoformat(timespec="seconds"),
        }
        _save(data)
    # Убираем из pending.
    remove_pending(chat_id)
    if existed:
        return True, f"обновлён: {chat_id} → {role}"
    return True, f"добавлен: {chat_id} → {role}"


def remove_user(chat_id: str) -> tuple[bool, str]:
    chat_id = str(chat_id)
    with _lock:
        data = _load()
        if chat_id not in data["users"]:
            return False, f"{chat_id} нет в списке"
        del data["users"][chat_id]
        _save(data)
    return True, f"удалён: {chat_id}"


def set_role(chat_id: str, role: str) -> tuple[bool, str]:
    chat_id = str(chat_id)
    if role not in ROLES:
        return False, f"неизвестная роль {role!r}"
    with _lock:
        data = _load()
        if chat_id not in data["users"]:
            return False, f"{chat_id} нет в списке"
        data["users"][chat_id]["role"] = role
        _save(data)
    return True, f"{chat_id} → {role}"


def list_users() -> list[dict]:
    with _lock:
        data = _load()
    out = []
    for cid, u in data["users"].items():
        out.append({
            "chat_id": cid,
            "role": u.get("role", "?"),
            "name": u.get("name", ""),
            "added_at": u.get("added_at", ""),
        })
    # Сначала админы.
    out.sort(key=lambda u: (u["role"] != "admin", u["chat_id"]))
    return out


# ──────────────────────────────────────────────────────────────
#  Pending
# ──────────────────────────────────────────────────────────────
def add_pending(chat_id: str, name: str, last_text: str = "") -> None:
    """Запоминает, что незнакомый писал боту."""
    chat_id = str(chat_id)
    if not chat_id:
        return
    with _lock:
        data = _read_json(PENDING_FILE, {"pending": {}})
        if "pending" not in data:
            data["pending"] = {}
        prev = data["pending"].get(chat_id) or {}
        data["pending"][chat_id] = {
            "name": name or prev.get("name", ""),
            "last_text": (last_text or "")[:100],
            "last_at": datetime.now().isoformat(timespec="seconds"),
            "attempts": int(prev.get("attempts", 0)) + 1,
        }
        _write_json(PENDING_FILE, data)


def remove_pending(chat_id: str) -> None:
    chat_id = str(chat_id)
    with _lock:
        data = _read_json(PENDING_FILE, {"pending": {}})
        if "pending" in data and chat_id in data["pending"]:
            del data["pending"][chat_id]
            _write_json(PENDING_FILE, data)


def list_pending() -> list[dict]:
    data = _read_json(PENDING_FILE, {"pending": {}})
    out = []
    for cid, p in (data.get("pending") or {}).items():
        out.append({
            "chat_id": cid,
            "name": p.get("name", ""),
            "last_text": p.get("last_text", ""),
            "last_at": p.get("last_at", ""),
            "attempts": p.get("attempts", 0),
        })
    out.sort(key=lambda x: x.get("last_at", ""), reverse=True)
    return out


# ──────────────────────────────────────────────────────────────
#  Приглашения
# ──────────────────────────────────────────────────────────────
import secrets as _secrets

INVITES_FILE = HERE / "bot_invites.json"
INVITE_TTL_DAYS = 7


def _load_invites() -> dict:
    data = _read_json(INVITES_FILE, {"invites": {}})
    if "invites" not in data or not isinstance(data["invites"], dict):
        data["invites"] = {}
    return data


def _save_invites(data: dict) -> None:
    _write_json(INVITES_FILE, data)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_invite(role: str, created_by: str, ttl_days: int = INVITE_TTL_DAYS
                  ) -> tuple[str, str]:
    """Создаёт одноразовый код. Возвращает (код, срок_действия_iso)."""
    if role not in ROLES:
        role = "viewer"
    code = _secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10]
    expires = datetime.fromtimestamp(
        datetime.now().timestamp() + ttl_days * 86400
    ).isoformat(timespec="seconds")
    with _lock:
        data = _load_invites()
        data["invites"][code] = {
            "role": role,
            "created_by": str(created_by),
            "created_at": _now_iso(),
            "expires_at": expires,
            "used_by": None,
            "used_at": None,
        }
        _save_invites(data)
    return code, expires


def use_invite(code: str, chat_id: str) -> tuple[bool, str, str]:
    """Применить код. Возвращает (ok, role, msg)."""
    code = (code or "").strip()
    if not code:
        return False, "", "пустой код"
    chat_id = str(chat_id)
    with _lock:
        data = _load_invites()
        inv = data["invites"].get(code)
        if not inv:
            return False, "", "код не найден"
        if inv.get("used_by"):
            return False, "", "код уже использован"
        # Проверка срока.
        try:
            exp = datetime.fromisoformat(inv.get("expires_at", ""))
            if exp < datetime.now():
                return False, "", "код истёк"
        except (ValueError, TypeError):
            pass
        role = inv.get("role", "viewer")
        # Добавляем пользователя (если ещё нет).
        users = _load()
        if chat_id in users["users"]:
            # Уже есть — обновляем только метку кода.
            pass
        else:
            users["users"][chat_id] = {
                "role": role,
                "name": "",
                "added_at": _now_iso(),
            }
            _save(users)
        # Помечаем код использованным.
        inv["used_by"] = chat_id
        inv["used_at"] = _now_iso()
        data["invites"][code] = inv
        _save_invites(data)
    return True, role, "ok"


def list_invites() -> list[dict]:
    data = _load_invites()
    out = []
    now = datetime.now()
    for code, inv in (data.get("invites") or {}).items():
        expired = False
        try:
            exp = datetime.fromisoformat(inv.get("expires_at", ""))
            expired = exp < now
        except (ValueError, TypeError):
            pass
        out.append({
            "code": code,
            "role": inv.get("role", "?"),
            "created_by": inv.get("created_by", ""),
            "created_at": inv.get("created_at", ""),
            "expires_at": inv.get("expires_at", ""),
            "used_by": inv.get("used_by"),
            "used_at": inv.get("used_at", ""),
            "expired": expired,
        })
    # Активные сверху.
    out.sort(key=lambda x: (bool(x["used_by"]) or x["expired"],
                             x["created_at"]), reverse=False)
    return out


def revoke_invite(code: str) -> tuple[bool, str]:
    code = (code or "").strip()
    with _lock:
        data = _load_invites()
        if code not in data["invites"]:
            return False, f"код {code!r} не найден"
        if data["invites"][code].get("used_by"):
            return False, f"код {code!r} уже использован, отозвать нельзя"
        del data["invites"][code]
        _save_invites(data)
    return True, f"код {code!r} отозван"


def can_run(chat_id: str, cmd: str) -> tuple[bool, str]:
    """Может ли пользователь выполнить команду.

    Возвращает (ok, reason).
    """
    role = get_role(chat_id)
    if role is None:
        return False, "not_in_list"
    if role == "admin":
        return True, "admin"
    if cmd in ADMIN_ONLY:
        return False, f"admin_only (ваша роль: {role})"
    if cmd in VIEWER_COMMANDS:
        return True, role
    # Неизвестная команда — разрешаем, но пусть handle_command решит.
    return True, role
