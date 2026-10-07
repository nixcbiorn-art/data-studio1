"""
tg_bot.py — Telegram-бот для отчётов по проблемным ЖК.
=========================================================
С кнопками:

  • Reply-клавиатура снизу — постоянные кнопки основных команд.
  • Inline-кнопки под /top, /find, /filter — каждая ЖК кликабельна,
    ведёт в карточку. Клик меняет текущее сообщение.

Запуск:
    python tg_bot.py
    python tg_bot.py --watch
    python tg_bot.py --send-only "текст"

Требуется:
    pip install requests
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import logging
import requests


# ──────────────────────────────────────────────────────────────
#  Пути и константы
# ──────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"
REPORTS = HERE / "reports"
EXPORT_SCRIPT = HERE / "export_problem_jc.py"
EXPORT_LOG = HERE / "tg_bot_export.log"
BOT_LOG = HERE / "tg_bot.log"
STATE = HERE / "tg_bot_state.json"


# Роли и права пользователей.
try:
    import bot_users
except ImportError:
    bot_users = None


# Имя бота — заполняется в poll_loop после getMe,
# нужно для ссылок-приглашений.
_BOT_USERNAME = ""
BOT_STARTED_AT = datetime.now()

API = "https://api.telegram.org"
POLL_TIMEOUT = 25
REQUEST_TIMEOUT = 30
WATCH_INTERVAL = 30
WATCH_BASELINE_MTIME_OFFSET = 5

# Кэш последних карточек для inline-кнопок. Индекс в списке идёт
# в callback_data. После перезапуска бота старые кнопки перестанут
# работать — это нормально, у пользователя всегда есть /find и /top.
_CARDS_CACHE: list = []


# ──────────────────────────────────────────────────────────────
#  Reply-клавиатура снизу
# ──────────────────────────────────────────────────────────────
# Кнопки приходят как обычный текст — то есть как /status, /top и т.д.
# Значит, отдельный маппинг не нужен, handle_command их разберёт сам.
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 /status"}, {"text": "🔝 /top 10"}],
        [{"text": "⚙️ /settings"}, {"text": "📡 /run_status"}],
        [{"text": "📋 /fill"},  {"text": "📈 /html"}],
        [{"text": "🔄 /report"}, {"text": "📁 /files"}],
        [{"text": "🔀 /diff"},   {"text": "❓ /help"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

# Reply-клавиатура — разная для admin и viewer.
MAIN_KEYBOARD_VIEWER = {
    "keyboard": [
        [{"text": "📊 /status"}, {"text": "🔝 /top 10"}],
        [{"text": "🔍 /find"},  {"text": "📋 /fill"}],
        [{"text": "📁 /files"}, {"text": "🔀 /diff"}],
        [{"text": "❓ /help"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def main_keyboard(role: str = "viewer") -> dict:
    """Возвращает reply-клавиатуру для роли."""
    if role == "admin":
        return MAIN_KEYBOARD
    return MAIN_KEYBOARD_VIEWER



# ──────────────────────────────────────────────────────────────
#  Логгер
# ──────────────────────────────────────────────────────────────
def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("tg_bot")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s  %(levelname)-5s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    try:
        fh = RotatingFileHandler(str(BOT_LOG), maxBytes=5 * 1024 * 1024,
                                 backupCount=1, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    try:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    except Exception:
        pass
    return logger


LOG = _setup_logger()


def log_startup_environment() -> None:
    try:
        from platform import python_version, system, release
        LOG.info("=" * 60)
        LOG.info("Запуск tg_bot.py")
        LOG.info("  Python: %s (%s %s)", python_version(), system(), release())
        LOG.info("  Cwd:    %s", HERE)
        LOG.info("  PID:    %s", os.getpid())
        LOG.info("  Лог:    %s", BOT_LOG)
        LOG.info("=" * 60)
    except Exception as e:
        LOG.warning("не удалось записать окружение: %s", e)


# ──────────────────────────────────────────────────────────────
#  .env и конфиг
# ──────────────────────────────────────────────────────────────
def _read_env() -> dict:
    values: dict = {}
    if not ENV.exists():
        return values
    for line in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def _get_conf():
    env = _read_env()
    token = (env.get("TELEGRAM_BOT_TOKEN")
             or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env.get("TELEGRAM_CHAT_ID")
               or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    extra = (env.get("TELEGRAM_EXTRA_CHATS")
             or os.environ.get("TELEGRAM_EXTRA_CHATS") or "").strip()
    return token, chat_id, [c.strip() for c in extra.split(",") if c.strip()]


# ──────────────────────────────────────────────────────────────
#  Telegram API
# ──────────────────────────────────────────────────────────────
def _api_call(method: str, token: str, **payload) -> dict | None:
    url = f"{API}/bot{token}/{method}"
    try:
        r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        LOG.warning("%s: %s: %s", method, type(e).__name__, e)
        return None
    try:
        data = r.json()
    except ValueError:
        LOG.warning("%s: ответ не JSON (HTTP %s)", method, r.status_code)
        return None
    if not data.get("ok"):
        LOG.warning("%s: Telegram отклонил: %s",
                    method, data.get("description"))
        return None
    return data


def _split_text(s: str, limit: int) -> list[str]:
    if len(s) <= limit:
        return [s]
    parts, buf = [], s
    while buf:
        if len(buf) <= limit:
            parts.append(buf)
            break
        cut = buf.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(buf[:cut])
        buf = buf[cut:].lstrip("\n")
    return parts


def send_message(token: str, chat_id: str, text: str,
                 parse_mode: str | None = "HTML",
                 reply_markup: dict | None = None) -> bool:
    chunks = _split_text(text, 4000)
    ok_all = True
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk,
                   "disable_web_page_preview": "true"}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        # reply_markup — только на первой части, чтобы не дублировать.
        if reply_markup and i == 0:
            payload["reply_markup"] = json.dumps(reply_markup)
        ok_all &= bool(_api_call("sendMessage", token, **payload))
    return ok_all


def edit_message_text(token: str, chat_id: str, message_id: int, text: str,
                      reply_markup: dict | None = None) -> bool:
    payload = {"chat_id": chat_id, "message_id": message_id,
               "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": "true"}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    return bool(_api_call("editMessageText", token, **payload))


_SILENT_CALLBACK_ERRORS = (
    "query is too old",
    "response timeout expired",
    "query ID is invalid",
    "message is not modified",
)


def answer_callback(token: str, cb_id: str, text: str = "") -> None:
    """Отвечает на callback. Молча глотает «устаревшие» ошибки."""
    payload = {"callback_query_id": cb_id}
    if text:
        payload["text"] = text
    url = f"{API}/bot{token}/answerCallbackQuery"
    try:
        r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        LOG.warning("answerCallbackQuery: %s: %s", type(e).__name__, e)
        return
    try:
        data = r.json()
    except ValueError:
        return
    if data.get("ok"):
        return
    desc = str(data.get("description") or "")
    for frag in _SILENT_CALLBACK_ERRORS:
        if frag in desc:
            LOG.debug("answerCallbackQuery: %s (тихо)", desc)
            return
    LOG.warning("answerCallbackQuery: %s", desc)


def send_document(token: str, chat_id: str, path: Path,
                  caption: str = "") -> bool:
    if not path.exists():
        return False
    url = f"{API}/bot{token}/sendDocument"
    try:
        with path.open("rb") as fh:
            r = requests.post(url, data={
                "chat_id": chat_id, "caption": caption[:1000],
                "parse_mode": "HTML",
            }, files={"document": (path.name, fh)}, timeout=60)
    except requests.RequestException as e:
        LOG.warning("sendDocument: %s: %s", type(e).__name__, e)
        return False
    try:
        data = r.json()
    except ValueError:
        return False
    if not data.get("ok"):
        LOG.warning("sendDocument: %s", data.get("description"))
        return False
    return True


def _html_escape(s) -> str:
    return (str(s or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


# ──────────────────────────────────────────────────────────────
#  Локальные файлы и разбор отчёта
# ──────────────────────────────────────────────────────────────
def _find_latest(pattern: str) -> Path | None:
    if not REPORTS.exists():
        return None
    files = sorted(REPORTS.glob(pattern),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _parse_md_summary(md_path: Path) -> dict:
    out = {"problems": 0, "discounts": 0, "by_dev": {}, "top": []}
    if not md_path.exists():
        return out
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    m = re.search(r"##\s*Проблемы\s*\((\d+)\s*ЖК\)", text)
    if m:
        out["problems"] = int(m.group(1))
    m = re.search(r"##\s*Скидочные расхождения\s*\((\d+)\s*ЖК\)", text)
    if m:
        out["discounts"] = int(m.group(1))
    block = ""
    m = re.search(r"##\s*Проблемы\s*\([^)]+\)(.*?)(?=\n##\s|\Z)", text, re.S)
    if m:
        block = m.group(1)
    by_dev: dict = {}
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if cells[0].lower() in ("застройщик", "---") or cells[0].startswith("-"):
            continue
        dev, jc = cells[0], cells[1]
        dpct = cells[3] if len(cells) > 3 else ""
        by_dev[dev] = by_dev.get(dev, 0) + 1
        if len(out["top"]) < 20:
            out["top"].append((dev, jc, dpct))
    out["by_dev"] = by_dev
    return out


def _parse_md_full(md_path: Path) -> list:
    if not md_path.exists():
        return []
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    m = re.search(r"^##\s+Детали\s*$", text, re.M)
    body = text[m.end():] if m else text
    chunks = re.split(r"(?m)^### ", body)
    return [c for c in (_parse_one_card(ch) for ch in chunks[1:]) if c]


def _parse_one_card(chunk: str):
    lines = chunk.splitlines()
    if not lines:
        return None
    head = lines[0].strip()
    cls = "small"
    if head.startswith("🔴"):
        cls = "crit"
    elif head.startswith("⚠️"):
        cls = "medium"
    elif head.startswith("✅"):
        cls = "small"
    m = re.match(r"^\S+\s+\[([^\]]+)\]\s+(.+)$", head)
    if not m:
        return None
    dev, jc = m.group(1).strip(), m.group(2).strip()
    diag: list = []
    metrics: list = []
    rows_rc = rows_src = 0
    dpct = "—"
    in_metrics = False
    for line in lines[1:]:
        s = line.strip()
        m1 = re.match(r"^-\s*строк:\s*RC\s*(\d+)\s*/\s*источник\s*(\d+)", s)
        if m1:
            rows_rc, rows_src = int(m1.group(1)), int(m1.group(2))
            continue
        if s.startswith("- "):
            diag.append(s[2:].strip())
            continue
        if s.startswith("#### "):
            in_metrics = False
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not cells:
                continue
            if cells[0].lower().startswith("метрик"):
                in_metrics = True
                continue
            if cells[0].startswith("---"):
                continue
            if in_metrics and len(cells) >= 5:
                metrics.append(tuple(cells[:5]))
                if cells[4] not in ("—", ""):
                    dpct = cells[4]
    return {"dev": dev, "jc": jc, "cls": cls,
            "rows_rc": rows_rc, "rows_src": rows_src,
            "dpct": dpct, "diag": diag, "metrics": metrics}


def _find_in_report(md_path: Path, needle: str, limit: int = 15) -> list:
    needle = (needle or "").strip().lower()
    if not needle:
        return []
    out = []
    for card in _parse_md_full(md_path):
        if needle in (card["dev"] + " " + card["jc"]).lower():
            out.append(card)
            if len(out) >= limit:
                break
    return out


def _filter_cards(cards: list, expr: str) -> list:
    if not expr.strip():
        return cards

    def _abs_pct(s):
        try:
            return abs(float(s.replace("%", "").replace(",", ".")
                            .replace("+", "").strip()))
        except (ValueError, AttributeError):
            return None

    out = cards
    for f in [p.strip() for p in expr.split(",") if p.strip()]:
        if f.startswith("dev:"):
            v = f[4:].strip().lower()
            out = [c for c in out if v in c["dev"].lower()]
        elif f.startswith("class:"):
            v = f[6:].strip().lower()
            v = {"крит": "crit", "критич": "crit",
                 "сред": "medium", "мелк": "small"}.get(v, v)
            out = [c for c in out if c["cls"] == v]
        elif f.startswith("min:"):
            try:
                n = float(f[4:].replace(",", "."))
                out = [c for c in out if (_abs_pct(c["dpct"]) or 0) >= n]
            except ValueError:
                pass
        elif f.startswith("max:"):
            try:
                n = float(f[4:].replace(",", "."))
                out = [c for c in out if (_abs_pct(c["dpct"]) or 0) <= n]
            except ValueError:
                pass
    return out


def _diff_reports() -> str:
    if not REPORTS.exists():
        return "Папка reports/ не найдена."
    files = sorted(REPORTS.glob("problem_jc_*.md"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if len(files) < 2:
        return "Есть только один отчёт — сравнивать не с чем."
    new_path, old_path = files[0], files[1]
    new_cards, old_cards = _parse_md_full(new_path), _parse_md_full(old_path)
    key = lambda c: (c["dev"], c["jc"])
    old_map = {key(c): c for c in old_cards}
    new_map = {key(c): c for c in new_cards}
    appeared = [c for k, c in new_map.items() if k not in old_map]
    gone = [c for k, c in old_map.items() if k not in new_map]
    changed = []
    for k in new_map.keys() & old_map.keys():
        a, b = old_map[k], new_map[k]
        if (a["dpct"] != b["dpct"] or a["rows_rc"] != b["rows_rc"]
                or a["rows_src"] != b["rows_src"]):
            changed.append((a, b))
    when = lambda p: datetime.fromtimestamp(
        p.stat().st_mtime).strftime("%d.%m %H:%M")
    lines = [
        "<b>Сравнение отчётов</b>",
        f"<i>было</i>: {_html_escape(old_path.name)} ({when(old_path)})",
        f"<i>стало</i>: {_html_escape(new_path.name)} ({when(new_path)})",
        "",
        f"Проблем в старом: <b>{len(old_cards)}</b>",
        f"Проблем в новом: <b>{len(new_cards)}</b>",
    ]
    if appeared:
        lines += ["", f"🆕 <b>Появились ({len(appeared)}):</b>"]
        for c in appeared[:10]:
            lines.append(f"  • {_html_escape(c['jc'])} "
                         f"({_html_escape(c['dev'])}) — Δ "
                         f"{_html_escape(c['dpct'])}")
        if len(appeared) > 10:
            lines.append(f"  … и ещё {len(appeared) - 10}")
    if gone:
        lines += ["", f"✅ <b>Ушли ({len(gone)}):</b>"]
        for c in gone[:10]:
            lines.append(f"  • {_html_escape(c['jc'])} "
                         f"({_html_escape(c['dev'])})")
        if len(gone) > 10:
            lines.append(f"  … и ещё {len(gone) - 10}")
    if changed:
        lines += ["", f"📊 <b>Изменились ({len(changed)}):</b>"]
        for a, b in changed[:10]:
            lines.append(f"  • {_html_escape(b['jc'])}: "
                         f"Δ {_html_escape(a['dpct'])} → "
                         f"{_html_escape(b['dpct'])}")
        if len(changed) > 10:
            lines.append(f"  … и ещё {len(changed) - 10}")
    if not (appeared or gone or changed):
        lines += ["", "Изменений нет — те же ЖК с теми же цифрами."]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
#  Форматирование карточки (общая функция для /jc и callback)
# ──────────────────────────────────────────────────────────────
def _format_card_full(c: dict) -> str:
    icon = {"crit": "🔴", "medium": "⚠️",
            "small": "·"}.get(c["cls"], "·")
    lines = [f"{icon} <b>{_html_escape(c['jc'])}</b>",
             f"<i>{_html_escape(c['dev'])}</i>",
             "",
             f"строк: RC {c['rows_rc']} / источник {c['rows_src']}"]
    if c["diag"]:
        lines += ["", "<b>Что странно:</b>"]
        for d in c["diag"]:
            lines.append(f"• {_html_escape(d)}")
    if c["metrics"]:
        lines += ["", "<b>Метрики:</b>"]
        for (label, rc, src, _dabs, dpct) in c["metrics"]:
            lines.append(f"• {_html_escape(label)}: "
                         f"RC {_html_escape(rc)} / ист {_html_escape(src)} "
                         f"— Δ {_html_escape(dpct)}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
#  Экспорт
# ──────────────────────────────────────────────────────────────
_LAST_EXPORT_ERROR = ""


def _subprocess_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_export():
    global _LAST_EXPORT_ERROR
    _LAST_EXPORT_ERROR = ""
    if not EXPORT_SCRIPT.exists():
        _LAST_EXPORT_ERROR = f"не найден {EXPORT_SCRIPT}"
        LOG.warning(_LAST_EXPORT_ERROR)
        return False, None, None, None
    LOG.info("export запущен: %s", EXPORT_SCRIPT.name)
    print(f"  ▶ запускаю {EXPORT_SCRIPT.name}…")
    try:
        res = subprocess.run(
            [sys.executable, str(EXPORT_SCRIPT)],
            cwd=str(HERE), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env=_subprocess_env(),
            timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as e:
        _LAST_EXPORT_ERROR = f"{type(e).__name__}: {e}"
        LOG.warning("export не запустился: %s", _LAST_EXPORT_ERROR)
        return False, None, None, None
    try:
        EXPORT_LOG.write_text(
            f"=== export_problem_jc.py ===\n"
            f"returncode: {res.returncode}\n"
            f"--- stdout ---\n{res.stdout}\n"
            f"--- stderr ---\n{res.stderr}\n",
            encoding="utf-8")
    except OSError:
        pass
    LOG.info("export завершён, код %s, stdout %d симв., stderr %d симв.",
             res.returncode, len(res.stdout or ""), len(res.stderr or ""))
    if res.returncode != 0:
        tail = (res.stderr or "").strip() or (res.stdout or "").strip() \
            or f"(пустой вывод, код {res.returncode})"
        _LAST_EXPORT_ERROR = tail[-1500:]
        LOG.warning("export провалился: %s", _LAST_EXPORT_ERROR)
        return False, None, None, None
    md = _find_latest("problem_jc_*.md")
    csv = _find_latest("problem_jc_*.csv")
    disc = _find_latest("problem_jc_*_discounts.csv")
    return True, md, csv, disc


# ──────────────────────────────────────────────────────────────
#  Тексты и сборка клавиатур
# ──────────────────────────────────────────────────────────────
def build_status_text() -> str:
    md = _find_latest("problem_jc_*.md")
    started = BOT_STARTED_AT.strftime("%d.%m.%Y %H:%M")
    if not md:
        return ("<b>Отчёта ещё нет.</b>\n"
                "Запустите <code>/report</code>, чтобы построить его.\n\n"
                f"<i>Бот запущен: {started}</i>")
    info = _parse_md_summary(md)
    when = datetime.fromtimestamp(md.stat().st_mtime).strftime("%d.%m.%Y %H:%M")
    lines = [
        "<b>Последний отчёт</b>",
        f"<i>{_html_escape(md.name)}</i> · {when}",
        "",
        f"🔧 Проблем: <b>{info['problems']}</b>",
        f"💸 Скидочных: {info['discounts']}",
        f"<i>Бот запущен: {started}</i>",
    ]
    if info["by_dev"]:
        lines += ["", "<b>По застройщикам:</b>"]
        for dev, n in sorted(info["by_dev"].items(),
                             key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  • {_html_escape(dev)} — {n}")
    return "\n".join(lines)


def build_top_text(n: int = 10) -> str:
    md = _find_latest("problem_jc_*.md")
    if not md:
        return "Отчёта ещё нет. Сначала <code>/report</code>."
    info = _parse_md_summary(md)
    if not info["top"]:
        return "В последнем отчёте проблем нет. ✅"
    lines = [f"<b>Топ-{min(n, len(info['top']))} проблемных ЖК</b>",
             "<i>Нажмите на ЖК под сообщением — покажу карточку.</i>", ""]
    for i, (dev, jc, dpct) in enumerate(info["top"][:n], 1):
        lines.append(f"{i}. <b>{_html_escape(jc)}</b> "
                     f"({_html_escape(dev)}) — Δ {_html_escape(dpct)}")
    return "\n".join(lines)


def build_report_caption(md_path: Path) -> str:
    info = _parse_md_summary(md_path)
    return (f"🔧 Проблем: <b>{info['problems']}</b>, "
            f"💸 скидочных: {info['discounts']}. "
            f"Файл: {_html_escape(md_path.name)}")


HELP_TEXT = (
    "<b>Что я умею</b>\n"
    "Внизу — кнопки для основных команд. Полный список:\n"
    "\n"
    "<b>Сбор и отчёты</b>\n"
    "/settings — настроить и запустить сбор\n"
    "/run — быстрый запуск с текущими настройками\n"
    "/run_status — что с текущим сбором\n"
    "/status — сводка по последнему отчёту\n"
    "/report — построить отчёт из уже собранного\n"
    "/top N — топ-N проблемных ЖК\n"
    "/diff — что изменилось с предыдущего отчёта\n"
    "\n"
    "<b>Поиск и разбор</b>\n"
    "/find текст — найти ЖК по названию или застройщику\n"
    "/jc название — карточка одного ЖК\n"
    "/filter dev:ЛСР — фильтр (dev: / class: / min: / max:)\n"
    "\n"
    "<b>Служебное</b>\n"
    "/files — свежие файлы в reports/\n/html — HTML-отчёты\n"
    "/log N — последние N строк tg_bot.log\n"
    "/ping — жив ли я\n"
    "/hidekbd — скрыть клавиатуру снизу\n"
    "/showkbd — вернуть клавиатуру\n"
    "/help — эта справка\n"
)


# ──────────────────────────────────────────────────────────────
#  Inline-клавиатура из карточек
# ──────────────────────────────────────────────────────────────
def _build_inline_from_cards(cards: list, max_buttons: int = 8) -> dict | None:
    """Кладёт карточки в _CARDS_CACHE и возвращает inline_keyboard."""
    global _CARDS_CACHE
    if not cards:
        return None
    _CARDS_CACHE = list(cards)
    rows = []
    for i, c in enumerate(cards[:max_buttons]):
        title = c["jc"]
        if len(title) > 40:
            title = title[:39] + "…"
        icon = {"crit": "🔴", "medium": "⚠️", "small": "·"}.get(c["cls"], "")
        text = f"{icon} {i + 1}. {title}"
        rows.append([{"text": text, "callback_data": f"jc:{i}"}])
    return {"inline_keyboard": rows}


# ──────────────────────────────────────────────────────────────
#  Обработчик команд
# ──────────────────────────────────────────────────────────────
def _reply(token: str, chat_id: str, text: str,
           reply_markup: dict | None = None) -> None:
    send_message(token, chat_id, text, parse_mode="HTML",
                 reply_markup=reply_markup)


# ──────────────────────────────────────────────────────────────
#  Запуск сбора (redcat_scraper.py)
# ──────────────────────────────────────────────────────────────
# Сбор — длинный процесс (минуты). Запускаем его в фоне: Popen пишет
# stdout/stderr в tg_run.log, отдельный поток ждёт завершения и потом
# шлёт инициатору короткий итог. Состояние запуска хранится в
# tg_run_state.json, чтобы /run_status отвечал даже между перезапусками
# бота. Кнопка «▶️ /run» добавляет аргументы по умолчанию: --only apartments,
# чтобы не гонять всю коллекцию. Передать свои аргументы — /run с текстом.
SCRAPER_SCRIPT = HERE / "redcat_scraper.py"
SCRAPER_LOG = HERE / "tg_run.log"
RUN_STATE = HERE / "tg_run_state.json"
_SCRAPER_PROC = None          # Popen текущего запуска (в этом процессе бота)
_SCRAPER_LOCK = threading.Lock()


def _load_run_state() -> dict:
    if not RUN_STATE.exists():
        return {}
    try:
        return json.loads(RUN_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_run_state(state: dict) -> None:
    try:
        RUN_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    except OSError:
        pass


def _process_alive(pid: int) -> bool:
    """Проверяет, жив ли процесс с данным PID.

    На Windows os.kill(pid, 0) НЕ «проверяет» — он вызывает
    TerminateProcess и УБИВАЕТ процесс. Поэтому на Windows
    используем OpenProcess + GetExitCodeProcess через ctypes.
    На POSIX os.kill(pid, 0) — штатный способ.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(
                        handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # Если ctypes не сработал — считаем, что процесса нет,
            # чтобы не запускать второй сбор поверх.
            return False

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _scraper_is_running() -> bool:
    """Живой ли сейчас запуск. Проверяем и объект, и state-файл."""
    with _SCRAPER_LOCK:
        if _SCRAPER_PROC is not None and _SCRAPER_PROC.poll() is None:
            return True
    st = _load_run_state()
    if not st.get("running"):
        return False
    if not _process_alive(st.get("pid")):
        st["running"] = False
        _save_run_state(st)
        return False
    return True


def _scraper_monitor(proc, chat_id: str, token: str) -> None:
    """Ждёт завершения subprocess, обновляет state, шлёт итог."""
    try:
        code = proc.wait()
    except Exception as e:
        LOG.exception("monitor: proc.wait упал: %s", e)
        code = -1

    st = _load_run_state()
    st["running"] = False
    st["finished_at"] = datetime.now().isoformat(timespec="seconds")
    st["exit_code"] = code
    _save_run_state(st)

    elapsed = ""
    try:
        if st.get("started_at"):
            t0 = datetime.fromisoformat(st["started_at"])
            dt = (datetime.now() - t0).total_seconds()
            elapsed = f" ({dt:.0f} сек)"
    except (ValueError, TypeError):
        pass

    # 3221225786 = 0xC000013A = STATUS_CONTROL_C_EXIT. Это Ctrl+C,
    # не реальная ошибка сбора.
    if code == 3221225786:
        head = f"⏹ <b>Сбор прерван</b> (Ctrl+C){elapsed}."
    elif code == 0:
        head = f"✅ <b>Сбор завершён</b>{elapsed}."
    else:
        head = f"❌ <b>Сбор упал</b> с кодом {code}{elapsed}."

    tail = ""
    if SCRAPER_LOG.exists():
        try:
            lines = SCRAPER_LOG.read_text(
                encoding="utf-8", errors="replace").splitlines()[-20:]
            tail = "\n".join(lines[-15:])
        except OSError:
            pass

    body = head
    if tail:
        body += f"\n\n<pre>{_html_escape(tail[-1500:])}</pre>"
    body += "\n\nНапишите /run_status для деталей."

    try:
        send_message(token, chat_id, body, parse_mode="HTML")
    except Exception as e:
        LOG.warning("monitor: не удалось отправить итог: %s", e)


def _scraper_start(chat_id: str, token: str, extra_args: list) -> tuple[bool, str]:
    """Запускает сбор. Возвращает (ok, текст_для_ответа)."""
    global _SCRAPER_PROC

    if not SCRAPER_SCRIPT.exists():
        return False, f"❌ Не найден <code>{SCRAPER_SCRIPT.name}</code>."

    if _scraper_is_running():
        st = _load_run_state()
        started = st.get("started_at") or "?"
        args = " ".join(st.get("args") or [])
        return False, (f"⏳ Сбор уже идёт.\n"
                       f"Начат: {_html_escape(started)}\n"
                       f"Аргументы: <code>{_html_escape(args or '—')}</code>\n"
                       f"Дождитесь завершения или посмотрите /run_status.")

    # Аргументы по умолчанию, если не передали ни одного: только квартиры.
    args = list(extra_args) if extra_args else ["--only", "apartments"]
    # Проверка admin-only команд.
    if bot_users is not None and cmd in bot_users.ADMIN_ONLY:
        if role != "admin":
            _reply(token, chat_id,
                   f"⛔ Команда <code>{cmd}</code> только для "
                   f"администраторов. Ваша роль: <b>{role}</b>.",
                   reply_markup=main_keyboard("viewer"))
            return

    # Пробуем писать stdout/stderr в файл, чтобы процесс не завис на PIPE.
    try:
        log_fh = open(SCRAPER_LOG, "w", encoding="utf-8", errors="replace")
    except OSError as e:
        return False, f"❌ Не могу открыть {SCRAPER_LOG.name}: {e}"

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(SCRAPER_SCRIPT), *args],
            cwd=str(HERE),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=_subprocess_env(),
            # Windows: своя консольная группа, чтобы Ctrl+C в окне
            # бота не убивал долгий сбор. На POSIX флаг отсутствует.
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt" else 0),
        
        )
    except OSError as e:
        log_fh.close()
        return False, f"❌ Не удалось запустить: {e}"

    with _SCRAPER_LOCK:
        _SCRAPER_PROC = proc

    st = {
        "running": True,
        "pid": proc.pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "exit_code": None,
        "args": args,
        "initiator": chat_id,
        "log_file": SCRAPER_LOG.name,
    }
    _save_run_state(st)

    threading.Thread(target=_scraper_monitor,
                     args=(proc, chat_id, token),
                     daemon=True).start()

    LOG.info("Запущен сбор: args=%s pid=%s", args, proc.pid)
    return True, (f"▶️ <b>Сбор запущен</b>\n"
                  f"PID: {proc.pid}\n"
                  f"Аргументы: <code>{_html_escape(' '.join(args))}</code>\n"
                  f"Лог: <code>{SCRAPER_LOG.name}</code>\n\n"
                  f"По завершении пришлю итог. Проверить вручную — "
                  f"/run_status.")


def _scraper_status_text() -> str:
    st = _load_run_state()
    if not st:
        return ("Сбор ещё не запускался. Запустить — /run "
                "(по умолчанию: только apartments).")
    running = bool(st.get("running")) and _scraper_is_running()
    lines = ["<b>Сбор</b>"]
    lines.append(f"Статус: " + ("⏳ идёт" if running else "✅ завершён"))
    if st.get("started_at"):
        lines.append(f"Начат: {_html_escape(st['started_at'])}")
    if st.get("finished_at") and not running:
        lines.append(f"Закончен: {_html_escape(st['finished_at'])}")
    if st.get("exit_code") is not None and not running:
        lines.append(f"Код возврата: {st['exit_code']}")
    if st.get("args"):
        lines.append(f"Аргументы: <code>{_html_escape(' '.join(st['args']))}</code>")
    if st.get("pid"):
        lines.append(f"PID: {st['pid']}")
    # Хвост лога.
    if SCRAPER_LOG.exists():
        try:
            tail = SCRAPER_LOG.read_text(
                encoding="utf-8", errors="replace").splitlines()[-15:]
            if tail:
                lines.append("")
                lines.append("<b>Хвост лога:</b>")
                lines.append(f"<pre>{_html_escape(chr(10).join(tail)[-1500:])}</pre>")
        except OSError:
            pass
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
#  Настройки сбора
# ──────────────────────────────────────────────────────────────
RUN_SETTINGS_FILE = HERE / "tg_run_settings.json"

DEFAULT_SETTINGS = {
    "sources": ["apartments"],
    "excel": False,
    "no_anomalies": False,
    "concurrency": 0,
    "sensitivity": 0.0,
}

CONCURRENCY_CYCLE = [0, 2, 4, 8, 16]
SENSITIVITY_CYCLE = [0.0, 2.5, 3.5, 5.0, 7.0]


def _load_settings() -> dict:
    if not RUN_SETTINGS_FILE.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(RUN_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)
    out = dict(DEFAULT_SETTINGS)
    out.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    if not isinstance(out.get("sources"), list):
        out["sources"] = list(DEFAULT_SETTINGS["sources"])
    out["sources"] = [str(s) for s in out["sources"] if s]
    return out


def _save_settings(s: dict) -> None:
    try:
        RUN_SETTINGS_FILE.write_text(
            json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        LOG.warning("не сохранил настройки: %s", e)


def _list_known_sources() -> list:
    out: list = []
    for folder, is_ext in ((HERE / "sources", False),
                           (HERE / "sources_external", True)):
        if not folder.exists():
            continue
        for p in sorted(folder.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = item.get("key")
                if not key:
                    continue
                if item.get("enabled", True) is False:
                    continue
                out.append({
                    "key": str(key),
                    "title": str(item.get("title") or key),
                    "external": bool(is_ext),
                })
    return out


def _settings_to_args(s: dict) -> list:
    args: list = []
    src = s.get("sources") or []
    if src:
        args.append("--only")
        args.extend(src)
    if s.get("excel"):
        args.append("--excel")
    if s.get("no_anomalies"):
        args.append("--no-anomalies")
    conc = int(s.get("concurrency") or 0)
    if conc > 0:
        args.extend(["--concurrency", str(conc)])
    sens = float(s.get("sensitivity") or 0)
    if sens > 0:
        args.extend(["--sensitivity", str(sens)])
    return args


def _settings_text(s: dict, sources_all: list) -> str:
    chosen = set(s.get("sources") or [])
    lines = ["<b>⚙️ Настройки сбора</b>", ""]
    lines.append("<b>Источники</b> (клик — вкл/выкл):")
    if not sources_all:
        lines.append("  <i>не нашёл ни одного описания в sources/</i>")
    else:
        for src in sources_all:
            mark = "✅" if src["key"] in chosen else "⬜"
            tag = " [внешний]" if src["external"] else ""
            lines.append(f"  {mark} <code>{_html_escape(src['key'])}</code>"
                         f" — {_html_escape(src['title'])}{tag}")
    if not chosen:
        lines.append("  <i>(ничего не выбрано — соберутся все)</i>")
    lines.append("")
    lines.append("<b>Опции</b>:")
    lines.append(f"  {'✅' if s.get('excel') else '⬜'} "
                 f"записать Excel (--excel)")
    lines.append(f"  {'✅' if s.get('no_anomalies') else '⬜'} "
                 f"без поиска аномалий (--no-anomalies)")
    lines.append("")
    conc = int(s.get("concurrency") or 0)
    sens = float(s.get("sensitivity") or 0)
    lines.append(f"Параллельность: <b>{conc if conc else 'по умолчанию'}</b>")
    lines.append(f"Чувствительность аномалий: "
                 f"<b>{sens if sens else 'по умолчанию'}</b>")
    lines.append("")
    lines.append("Когда всё готово — нажми «▶️ Запустить».")
    lines.append("Итоговые аргументы:")
    args = _settings_to_args(s)
    lines.append(f"<code>{_html_escape(' '.join(args) or '(без аргументов)')}</code>")
    return "\n".join(lines)


def _settings_keyboard(s: dict, sources_all: list, offset: int = 0,
                       page: int = 10) -> dict:
    chosen = set(s.get("sources") or [])
    rows: list = []
    total = len(sources_all)
    chunk = sources_all[offset:offset + page] if total > page else sources_all

    for src in chunk:
        key = src["key"]
        mark = "✅" if key in chosen else "⬜"
        label = f"{mark} {key}"
        if len(label) > 40:
            label = label[:39] + "…"
        rows.append([{"text": label,
                      "callback_data": f"set:toggle_src:{key}"}])

    if total > page:
        nav = []
        if offset > 0:
            nav.append({"text": "◀",
                        "callback_data": f"set:page:{offset - page}"})
        nav.append({"text": f"{offset // page + 1}/"
                            f"{(total + page - 1) // page}",
                    "callback_data": "set:noop"})
        if offset + page < total:
            nav.append({"text": "▶",
                        "callback_data": f"set:page:{offset + page}"})
        rows.append(nav)

    rows.append([
        {"text": ("✅" if s.get("excel") else "⬜") + " Excel",
         "callback_data": "set:toggle_opt:excel"},
        {"text": ("✅" if s.get("no_anomalies") else "⬜") + " без аномалий",
         "callback_data": "set:toggle_opt:no_anomalies"},
    ])
    rows.append([
        {"text": f"⚙️ concurrency: "
                 f"{int(s.get('concurrency') or 0) or 'по умолч.'}",
         "callback_data": "set:cycle_conc"},
    ])
    rows.append([
        {"text": f"⚙️ sensitivity: "
                 f"{float(s.get('sensitivity') or 0) or 'по умолч.'}",
         "callback_data": "set:cycle_sens"},
    ])
    rows.append([
        {"text": "▶️ Запустить", "callback_data": "set:run"},
        {"text": "🔄 Сбросить", "callback_data": "set:reset"},
    ])
    return {"inline_keyboard": rows}


# ──────────────────────────────────────────────────────────────
#  Заполненность данных (использует локальный completeness.py)
# ──────────────────────────────────────────────────────────────
# Тот же модуль, что и вкладка «Заполненность» приложения: считает
# заполненность по полям, по записям и взвешенную общую (обязательные
# поля весят больше опциональных). Работает и с redcat_data.db, и с
# external_data.db — по имени таблицы выбирается база.
#
# Расчёт не мгновенный: по полям — быстро, по записям — на десятках
# тысяч строк может занять секунды. Поэтому записи считаем только
# для таблиц поменьше (до 30000 строк), а для больших честно пишем,
# что детали смотри в приложении.
FILL_MAX_ROWS_FOR_RECORDS = 30000


def _load_specs_safe() -> dict:
    """Спеки источников. Нужны для весов и «обязательных» полей."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import sources as _src
        _src.load_from_dir(HERE / "sources")
        _src.load_from_dir(HERE / "sources_external")
        return {s.key: s for s in _src.all_sources()}
    except Exception as e:
        LOG.warning("fill: specs не загружены: %s", e)
        return {}


def _db_for_table(table: str) -> Path:
    """В какой базе лежит таблица: external или redcat."""
    rc = REPORTS / "redcat_data.db"
    ext = REPORTS / "external_data.db"
    try:
        import sqlite3 as _sq
        if ext.exists():
            conn = _sq.connect(f"file:{ext}?mode=ro", uri=True)
            try:
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
            if table in names:
                return ext
    except Exception as e:
        LOG.debug("fill: не смог открыть external: %s", e)
    return rc


def _fill_all_tables() -> list:
    """[{name, rows, db}] — все пользовательские таблицы из обеих баз."""
    out: list = []
    seen: set = set()
    for db in (REPORTS / "redcat_data.db", REPORTS / "external_data.db"):
        if not db.exists():
            continue
        try:
            import sqlite3 as _sq
            conn = _sq.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "ORDER BY name").fetchall()
            finally:
                conn.close()
        except Exception as e:
            LOG.warning("fill: %s — %s", db.name, e)
            continue
        for (name,) in rows:
            if (not name or name in seen or name.startswith("_")
                    or name.startswith("sqlite_")
                    or name == "comparison_vs_previous"):
                continue
            seen.add(name)
            try:
                import sqlite3 as _sq2
                c2 = _sq2.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    n = c2.execute(
                        f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                finally:
                    c2.close()
            except Exception:
                n = 0
            out.append({"name": name, "rows": n, "db": db})
    return out


def _fill_quick_overall(table: str, specs: dict, max_worst: int = 3) -> dict:
    """Быстрая сводка по таблице: только по полям (без обхода записей).

    Возвращает {"pct": float|None, "fields_total": int,
                "below50": int, "worst": [(field, rate), ...]}.
    """
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import completeness as _cm
    except Exception as e:
        return {"error": f"completeness недоступен: {e}"}

    db = _db_for_table(table)
    spec = specs.get(table)
    try:
        fields = _cm.completeness_by_field(db, table)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    for f in fields:
        f["kind"] = _cm._kind(f["field"], spec)
    overall = _cm.overall_score(fields, spec)
    below50 = sum(1 for f in fields if (f["fill_rate"] or 0) < 50)
    ordered = sorted(fields, key=lambda x: (x["fill_rate"] or 0))
    return {
        "pct": overall.get("pct"),
        "fields_total": len(fields),
        "below50": below50,
        "worst": [(f["field"], f["fill_rate"]) for f in ordered[:max_worst]],
        "fields": fields,
        "overall": overall,
    }


def _fill_table_detailed(table: str, specs: dict,
                         top_fields: int = 15, top_records: int = 8) -> str:
    """Детальный HTML по одной таблице: поля + записи + группы."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import completeness as _cm
    except Exception as e:
        return f"❌ completeness недоступен: {e}"

    db = _db_for_table(table)
    spec = specs.get(table)
    title = (spec.title if spec and getattr(spec, "title", "") else table)
    try:
        fields = _cm.completeness_by_field(db, table)
    except Exception as e:
        return f"❌ Не смог прочитать «{table}»: {type(e).__name__}: {e}"

    for f in fields:
        f["kind"] = _cm._kind(f["field"], spec)
    overall = _cm.overall_score(fields, spec)

    # Общая инфо-шапка.
    pct = overall.get("pct")
    pct_s = f"{pct:.1f}%" if pct is not None else "—"
    fields_total = overall.get("fields", len(fields))
    excluded = overall.get("excluded", 0)
    below50 = sum(1 for f in fields if (f["fill_rate"] or 0) < 50)

    lines = [f"📋 <b>Заполненность: {_html_escape(table)}</b>",
             f"<i>{_html_escape(title)}</i>", ""]
    lines.append(f"Взвешенная заполненность: <b>{pct_s}</b> "
                 f"по {fields_total} полям"
                 + (f" (исключено пустых: {excluded})" if excluded else ""))
    lines.append(f"Полей с заполненностью &lt; 50%: <b>{below50}</b>")

    # Топ худших полей.
    ordered = sorted(fields, key=lambda x: (x["fill_rate"] or 0))
    worst = ordered[:top_fields]
    if worst:
        lines.append("")
        lines.append(f"<b>Худшие поля (топ-{len(worst)})</b>:")
        for f in worst:
            mark = " *" if f.get("kind") == "required" else ""
            rate = f["fill_rate"]
            rate_s = f"{rate:.1f}%" if rate is not None else "—"
            lines.append(f"  • <code>{_html_escape(f['field'])}</code>{mark}"
                         f" — {rate_s}")

    # Записи — только для небольших таблиц.
    rows_count = 0
    try:
        import sqlite3 as _sq
        c = _sq.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows_count = c.execute(
                f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        finally:
            c.close()
    except Exception:
        pass

    if rows_count and rows_count > FILL_MAX_ROWS_FOR_RECORDS:
        lines.append("")
        lines.append(f"Записей: {rows_count:,}. "
                     "Записи по строкам не считаю (таблица большая). "
                     "Детали — во вкладке «Заполненность» приложения."
                     .replace(",", "\u202f"))
    else:
        try:
            records = _cm.completeness_by_record(
                db, table, spec, precomputed_fields=fields)
        except Exception as e:
            records = []
            LOG.warning("fill: записи по %s — %s", table, e)
        if records:
            below80 = [r for r in records if r["pct"] < 80]
            below80.sort(key=lambda r: r["pct"])
            lines.append("")
            lines.append(f"Записей: {len(records)}, из них ниже 80%: "
                         f"<b>{len(below80)}</b>")
            if below80:
                lines.append(f"<b>Худшие записи (топ-{min(top_records, len(below80))})</b>:")
                for r in below80[:top_records]:
                    miss = ", ".join(r.get("missing", [])[:4])
                    name = r.get("name") or r.get("id")
                    lines.append(
                        f"  • {_html_escape(str(name))} — "
                        f"{r['pct']:.1f}% (нет: {_html_escape(miss) or '—'})")

    lines.append("")
    lines.append("<i>* обязательное поле по описанию источника.</i>")
    return "\n".join(lines)


def _fill_summary_all(specs: dict) -> str:
    """Сводка по всем таблицам — только 3 худших поля на таблицу.

    Полные поля и записи — по /fill <table>. Сводка должна быть быстрой.
    """
    tables = _fill_all_tables()
    if not tables:
        return "❌ В reports/ нет собранных таблиц."

    lines = ["📋 <b>Заполненность данных</b>",
             "<i>Кликните на таблицу под сообщением — покажу детали.</i>",
             ""]
    for t in tables:
        info = _fill_quick_overall(t["name"], specs, max_worst=3)
        if "error" in info:
            lines.append(f"⚠️ <b>{_html_escape(t['name'])}</b> — "
                         f"{_html_escape(info['error'])}")
            continue
        pct = info.get("pct")
        pct_s = f"{pct:.1f}%" if pct is not None else "—"
        below50 = info.get("below50", 0)
        rows = t.get("rows", 0)
        worst = info.get("worst") or []
        worst_s = ", ".join(
            f"{_html_escape(w[0])} {w[1]:.0f}%"
            for w in worst if w[1] is not None
        ) or "—"
        lines.append(
            f"<b>{_html_escape(t['name'])}</b> "
            f"({rows:,} строк)".replace(",", "\u202f")
        )
        lines.append(
            f"   заполнено: <b>{pct_s}</b>, полей &lt; 50%: {below50}"
        )
        lines.append(f"   худшие: {worst_s}")
        lines.append("")
    return "\n".join(lines)


def _fill_keyboard() -> dict | None:
    """Inline-клавиатура со списком таблиц (по 1 в строке)."""
    tables = _fill_all_tables()
    if not tables:
        return None
    rows = []
    for t in tables[:20]:
        label = t["name"]
        if len(label) > 50:
            label = label[:49] + "…"
        rows.append([{"text": label,
                      "callback_data": f"fill:table:{t['name']}"}])
    rows.append([{"text": "🔄 Обновить",
                  "callback_data": "fill:refresh"}])
    return {"inline_keyboard": rows}


def _handle_fill_callback(token: str, chat_id: str, cb_id: str,
                          message_id, data: str) -> None:
    """Обрабатывает клики по кнопкам /fill."""
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""

    if action == "table" and len(parts) > 2:
        table = parts[2]
        answer_callback(token, cb_id, f"считаю «{table}»…")
        specs = _load_specs_safe()
        body = _fill_table_detailed(table, specs)
        # «Назад к списку» — кнопкой.
        kb = {"inline_keyboard": [[
            {"text": "◀ ко всем таблицам", "callback_data": "fill:refresh"}
        ]]}
        edit_message_text(token, chat_id, message_id, body, reply_markup=kb)
        return

    if action == "refresh":
        answer_callback(token, cb_id, "обновляю")
        specs = _load_specs_safe()
        body = _fill_summary_all(specs)
        kb = _fill_keyboard()
        edit_message_text(token, chat_id, message_id, body, reply_markup=kb)
        return

    answer_callback(token, cb_id, "неизвестное действие")


# ──────────────────────────────────────────────────────────────
#  Генерация HTML-отчётов
# ──────────────────────────────────────────────────────────────
# В проекте уже есть три скрипта, каждый строит самодостаточный HTML:
#   • history_dashboard.py      → reports/history.html
#   • completeness_dashboard.py → reports/completeness.html
#   • report_html.py            → reports/dashboard.html
#     (этот вызывается из redcat_scraper.py после каждого сбора)
#
# Все запускаются с --no-open, чтобы не открывать браузер на машине,
# где крутится бот.

HTML_REPORTS = {
    "history": {
        "script": "history_dashboard.py",
        "output": REPORTS / "history.html",
        "title": "История по источникам",
        "caption": "📊 История по источникам — динамика метрик "
                   "по каждому запуску",
    },
    "completeness": {
        "script": "completeness_dashboard.py",
        "output": REPORTS / "completeness.html",
        "title": "Заполненность данных",
        "caption": "📋 Заполненность — по полям, записям и группам",
    },
    "dashboard": {
        "script": None,   # не генерируем, только отдаём готовый
        "output": REPORTS / "dashboard.html",
        "title": "Дашборд сборщика",
        "caption": "📈 Дашборд — создаётся автоматически после "
                   "каждого запуска сбора",
    },
    "stats": {
        "script": "source_stats.py",
        "output": REPORTS / "source_stats.html",
        "title": "Сверка с Redcat",
        "caption": "⇄ Сверка источников с Redcat — расхождения "
                   "по метрикам и объёмам",
        "extra_args": ["--html", str(REPORTS / "source_stats.html")],
    },
}


def _html_generate(key: str) -> tuple[bool, str, Path | None]:
    """Запускает скрипт генерации. Возвращает (ok, message, path)."""
    cfg = HTML_REPORTS.get(key)
    if not cfg:
        return False, f"❌ Неизвестный отчёт: {key}", None

    out_path = cfg["output"]
    script_name = cfg["script"]

    # dashboard.html не генерируется этой командой — только отдаётся.
    if script_name is None:
        if not out_path.exists():
            return False, (f"❌ <code>{out_path.name}</code> ещё нет. "
                           f"Запусти сбор: /run."), None
        return True, "готово", out_path

    script_path = HERE / script_name
    if not script_path.exists():
        return False, f"❌ Не найден <code>{script_name}</code>.", None

    LOG.info("HTML: запускаю %s", script_name)
    t_start = datetime.now().timestamp()
    try:
        cmd = [sys.executable, str(script_path), "--no-open"]
        cmd += list(cfg.get("extra_args") or [])
        res = subprocess.run(
            cmd,
            cwd=str(HERE),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env=_subprocess_env(),
            timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"❌ Не удалось запустить: {e}", None

    # Успех определяется не кодом возврата, а фактом: файл существует и
    # обновился после нашего запуска. source_stats.py, например, отдаёт
    # код 2, когда в данных есть критичные расхождения — это не ошибка
    # генерации, файл при этом создан.
    now = datetime.now().timestamp()
    fresh = False
    try:
        fresh = out_path.exists() and out_path.stat().st_mtime >= t_start - 1
    except OSError:
        fresh = False

    if res.returncode != 0 and not fresh:
        # Реальная ошибка: и код ненулевой, и файл не обновился.
        tail = (res.stderr or "").strip() or (res.stdout or "").strip()
        tail = tail[-1200:] if tail else f"код {res.returncode}"
        LOG.warning("HTML %s упал: %s", script_name, tail[-300:])
        return False, (f"❌ Генерация не удалась.\n\n"
                       f"<pre>{_html_escape(tail)}</pre>"), None

    if not out_path.exists():
        return False, (f"❌ Скрипт отработал, но файл "
                       f"<code>{out_path.name}</code> не найден."), None

    size = out_path.stat().st_size
    note = ""
    if res.returncode != 0:
        # Мягкий ненулевой код (например, 2 у source_stats.py) —
        # файл всё равно готов и корректен.
        note = f" (код возврата {res.returncode} — есть замечания в данных)"
        LOG.info("HTML %s готов с ненулевым кодом %d (файл обновлён)",
                 out_path.name, res.returncode)
    else:
        LOG.info("HTML %s готов: %d байт", out_path.name, size)
    return True, note or "готово", out_path


def _html_list_files() -> str:
    """Список всех .html файлов в reports/."""
    if not REPORTS.exists():
        return "Папка reports/ не найдена."
    items = sorted(REPORTS.glob("*.html"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not items:
        return ("В reports/ нет ни одного HTML-файла.\n"
                "Сгенерировать: /html history, /html completeness.\n"
                "dashboard.html появляется после запуска сбора.")
    lines = ["<b>HTML-отчёты в reports/</b>", ""]
    for p in items[:20]:
        sz = p.stat().st_size
        unit = "Б"
        if sz >= 1024 * 1024:
            sz, unit = sz // (1024 * 1024), "МБ"
        elif sz >= 1024:
            sz, unit = sz // 1024, "КБ"
        when = datetime.fromtimestamp(
            p.stat().st_mtime).strftime("%d.%m %H:%M")
        lines.append(f"• <code>{_html_escape(p.name)}</code> — "
                     f"{sz} {unit}, {when}")
    lines.append("")
    lines.append("Скачать один: /html history, /html completeness, "
                 "/html dashboard.")
    return "\n".join(lines)


def _html_menu_text() -> str:
    lines = ["<b>HTML-отчёты</b>", ""]
    for key, cfg in HTML_REPORTS.items():
        out = cfg["output"]
        mark = "✅" if out.exists() else "⬜"
        title = cfg["title"]
        if out.exists():
            when = datetime.fromtimestamp(
                out.stat().st_mtime).strftime("%d.%m %H:%M")
            lines.append(f"  {mark} <b>{title}</b> — <i>{when}</i>")
        else:
            lines.append(f"  {mark} <b>{title}</b> — ещё не создан")
    lines.append("")
    lines.append("Кликните под сообщением, чтобы сгенерировать и получить "
                 "файл. dashboard.html создаётся сборщиком после /run.")
    return "\n".join(lines)


def _html_menu_keyboard() -> dict:
    rows = []
    for key, cfg in HTML_REPORTS.items():
        label = cfg["title"]
        mark = "✅" if cfg["output"].exists() else "⬜"
        rows.append([{"text": f"{mark} {label}",
                      "callback_data": f"html:send:{key}"}])
    rows.append([
        {"text": "📂 Все файлы",
         "callback_data": "html:list"},
        {"text": "🔄 Обновить",
         "callback_data": "html:menu"},
    ])
    return {"inline_keyboard": rows}


def _handle_html_callback(token: str, chat_id: str, cb_id: str,
                          message_id, data: str) -> None:
    """Клики по /html."""
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""

    if action == "menu":
        answer_callback(token, cb_id, "обновляю")
        edit_message_text(token, chat_id, message_id,
                          _html_menu_text(),
                          reply_markup=_html_menu_keyboard())
        return

    if action == "list":
        answer_callback(token, cb_id)
        edit_message_text(token, chat_id, message_id,
                          _html_list_files(),
                          reply_markup={"inline_keyboard": [[
                              {"text": "◀ назад",
                               "callback_data": "html:menu"}]]
                          })
        return

    if action == "send" and len(parts) > 2:
        key = parts[2]
        cfg = HTML_REPORTS.get(key)
        if not cfg:
            answer_callback(token, cb_id, "неизвестный отчёт")
            return
        # Для dashboard.html — просто отправляем файл.
        if cfg["script"] is None:
            answer_callback(token, cb_id, "отправляю…")
            if not cfg["output"].exists():
                answer_callback(token, cb_id, "файла ещё нет")
                _reply(token, chat_id,
                       "❌ dashboard.html ещё не создан. "
                       "Запусти /run — сборщик его построит.")
                return
            send_document(token, chat_id, cfg["output"],
                          caption=cfg["caption"])
            return

        answer_callback(token, cb_id,
                        f"генерирую «{cfg['title']}», подожди…")
        # Редактируем сообщение — показываем, что идёт работа.
        edit_message_text(
            token, chat_id, message_id,
            f"⏳ Генерирую <b>{_html_escape(cfg['title'])}</b>…\n"
            f"Это может занять 10–30 секунд.",
            reply_markup={"inline_keyboard": []})

        ok, msg, path = _html_generate(key)
        if not ok:
            edit_message_text(token, chat_id, message_id, msg,
                              reply_markup={"inline_keyboard": [[
                                  {"text": "◀ назад к меню",
                                   "callback_data": "html:menu"}]]})
            return

        # Успех — отправляем файл и обновляем меню.
        send_document(token, chat_id, path, caption=cfg["caption"])
        edit_message_text(token, chat_id, message_id,
                          _html_menu_text(),
                          reply_markup=_html_menu_keyboard())
        return

    answer_callback(token, cb_id, "неизвестное действие")


def handle_command(token: str, chat_id: str, text: str,
                   role: str = "viewer") -> None:
    # Проверка роли и прав (bot_users).
    if bot_users is not None:
        _role_now = bot_users.get_role(chat_id)
        if _role_now is None:
            # Может быть, это переход по приглашению:
            # /start <код> (deep link из ссылки t.me/...)
            _invited = False
            _s = (text or "").strip()
            if _s.startswith("/start"):
                _parts = _s.split(maxsplit=1)
                if len(_parts) > 1:
                    _code = _parts[1].strip()
                    _ok, _new_role, _msg = bot_users.use_invite(
                        _code, chat_id)
                    if _ok:
                        _role_now = _new_role
                        _invited = True
                        LOG.info("по приглашению %s: %s → %s",
                                 _code, chat_id, _new_role)
                        _reply(token, chat_id,
                               f"✅ Приглашение принято. Ваша роль: "
                               f"<b>{_new_role}</b>\n\n"
                               f"Напишите /help — покажу, что умею.",
                               reply_markup=main_keyboard(_new_role))
                    else:
                        LOG.warning("плохой код %r от %s: %s",
                                    _code, chat_id, _msg)
            if not _invited:
                bot_users.add_pending(chat_id, "", text)
                LOG.warning("сообщение от %s — не в списке", chat_id)
                return
        role = _role_now
    LOG.info("команда от %s: %s", chat_id, (text or "").strip()[:120])
    raw = (text or "").strip()
    # Reply-кнопки снизу подписаны с эмодзи: «📁 /files». Telegram
    # присылает текст КАК ЕСТЬ — то есть наша проверка startswith("/")
    # на них падала, и бот отвечал «Не понял». Поэтому ищем команду
    # в любом месте строки: первое /<буквы>, до конца строки — команда
    # и её аргументы. Всё, что было до этого (эмодзи, пробелы, «нажми»),
    # отбрасываем.
    m = re.search(r"/([A-Za-zА-Яа-яЁё_]+)(?:@\w+)?(?:\s+(.*))?$", raw)
    if not m:
        _reply(token, chat_id, "Не понял. /help — что я умею.")
        return
    cmd = "/" + m.group(1).lower()
    args = (m.group(2) or "").split()
    LOG.debug("разобрано: cmd=%r args=%r", cmd, args)

    if cmd in ("/start", "/help"):
        _reply(token, chat_id, HELP_TEXT, reply_markup=main_keyboard(role))

    elif cmd == "/showkbd":
        _reply(token, chat_id, "Клавиатура снизу вернулась.",
               reply_markup=main_keyboard(role))

    elif cmd == "/hidekbd":
        _reply(token, chat_id, "Клавиатура снизу скрыта. "
                               "Вернуть — /showkbd.",
               reply_markup={"remove_keyboard": True})

    elif cmd == "/ping":
        _reply(token, chat_id,
               f"🏓 Жив. Время сервера: {datetime.now():%d.%m.%Y %H:%M:%S}")

    elif cmd == "/status":
        _reply(token, chat_id, build_status_text())

    elif cmd == "/top":
        n = 10
        if args:
            try:
                n = max(1, min(int(args[0]), 25))
            except ValueError:
                pass
        md = _find_latest("problem_jc_*.md")
        if not md:
            _reply(token, chat_id,
                   "Отчёта ещё нет. Сначала <code>/report</code>.")
            return
        cards = _parse_md_full(md)[:n]
        if not cards:
            _reply(token, chat_id, "В последнем отчёте проблем нет. ✅")
            return
        kb = _build_inline_from_cards(cards, max_buttons=n)
        _reply(token, chat_id, build_top_text(n), reply_markup=kb)

    elif cmd == "/report":
        _reply(token, chat_id, "⏳ Запускаю сбор… Это может занять минуту.")
        LOG.info("/report начат (chat_id=%s)", chat_id)
        _t0 = time.time()
        ok, md, csv, disc = run_export()
        LOG.info("/report завершён: ok=%s, за %.1f сек",
                 ok, time.time() - _t0)
        if not ok:
            err = _LAST_EXPORT_ERROR or "(нет вывода)"
            if len(err) > 3500:
                err = err[-3500:]
            _reply(token, chat_id,
                   "❌ <b>Не удалось построить отчёт.</b>\n\n"
                   f"<pre>{_html_escape(err)}</pre>\n\n"
                   f"Полный лог: <code>{_html_escape(EXPORT_LOG.name)}</code>")
            return
        if md:
            send_document(token, chat_id, md, caption=build_report_caption(md))
        if csv:
            send_document(token, chat_id, csv, caption="Список проблем (CSV)")
        if disc:
            send_document(token, chat_id, disc, caption="Скидочные (CSV)")
        _reply(token, chat_id, build_status_text(), reply_markup=main_keyboard(role))

    elif cmd == "/diff":
        _reply(token, chat_id, _diff_reports())

    elif cmd == "/find":
        q = " ".join(args).strip()
        if not q:
            _reply(token, chat_id, "Укажите текст: <code>/find ЗИЛАРТ</code>")
            return
        md = _find_latest("problem_jc_*.md")
        if not md:
            _reply(token, chat_id,
                   "Отчёта ещё нет. Сначала <code>/report</code>.")
            return
        cards = _find_in_report(md, q, limit=15)
        if not cards:
            _reply(token, chat_id,
                   f"Ничего не найдено по «{_html_escape(q)}».")
            return
        lines = [f"<b>Найдено: {len(cards)}</b>",
                 "<i>Нажмите на ЖК под сообщением — покажу карточку.</i>",
                 ""]
        for i, c in enumerate(cards, 1):
            icon = {"crit": "🔴", "medium": "⚠️",
                    "small": "·"}.get(c["cls"], "·")
            lines.append(f"{i}. {icon} <b>{_html_escape(c['jc'])}</b> "
                         f"({_html_escape(c['dev'])}) — Δ "
                         f"{_html_escape(c['dpct'])}")
        kb = _build_inline_from_cards(cards)
        _reply(token, chat_id, "\n".join(lines), reply_markup=kb)

    elif cmd == "/jc":
        q = " ".join(args).strip()
        if not q:
            _reply(token, chat_id,
                   "Укажите название: <code>/jc ЗИЛАРТ</code>")
            return
        md = _find_latest("problem_jc_*.md")
        if not md:
            _reply(token, chat_id,
                   "Отчёта ещё нет. Сначала <code>/report</code>.")
            return
        cards = _find_in_report(md, q, limit=3)
        if not cards:
            _reply(token, chat_id, f"Не нашёл ЖК по «{_html_escape(q)}».")
            return
        _reply(token, chat_id, _format_card_full(cards[0]))
        if len(cards) > 1:
            # Похожие — inline-кнопки, чтобы кликнуть в один тап.
            kb = _build_inline_from_cards(cards[1:], max_buttons=3)
            _reply(token, chat_id,
                   "<i>Похожие:</i>",
                   reply_markup=kb)

    elif cmd == "/filter":
        expr = " ".join(args).strip()
        md = _find_latest("problem_jc_*.md")
        if not md:
            _reply(token, chat_id,
                   "Отчёта ещё нет. Сначала <code>/report</code>.")
            return
        cards = _parse_md_full(md)
        if not expr:
            _reply(token, chat_id,
                   "Фильтр пустой. Примеры:\n"
                   "<code>/filter dev:ЛСР</code>\n"
                   "<code>/filter class:crit</code>\n"
                   "<code>/filter min:10</code>\n"
                   "Можно через запятую: "
                   "<code>/filter dev:А101,min:5</code>")
            return
        picked = _filter_cards(cards, expr)
        if not picked:
            _reply(token, chat_id,
                   f"Ничего не подходит под «{_html_escape(expr)}».")
            return
        lines = [f"<b>Подходит: {len(picked)} из {len(cards)}</b>",
                 "<i>Нажмите на ЖК — покажу карточку.</i>", ""]
        for i, c in enumerate(picked[:25], 1):
            icon = {"crit": "🔴", "medium": "⚠️",
                    "small": "·"}.get(c["cls"], "·")
            lines.append(f"{i}. {icon} {_html_escape(c['jc'])} "
                         f"({_html_escape(c['dev'])}) — Δ "
                         f"{_html_escape(c['dpct'])}")
        if len(picked) > 25:
            lines.append(f"… и ещё {len(picked) - 25}")
        kb = _build_inline_from_cards(picked)
        _reply(token, chat_id, "\n".join(lines), reply_markup=kb)

    elif cmd == "/settings":
        s = _load_settings()
        srcs = _list_known_sources()
        _reply(token, chat_id, _settings_text(s, srcs),
               reply_markup=_settings_keyboard(s, srcs))

    elif cmd == "/run":
        if not args:
            args = _settings_to_args(_load_settings())
        ok, msg = _scraper_start(chat_id, token, args)
        _reply(token, chat_id, msg)

    elif cmd == "/run_status":
        _reply(token, chat_id, _scraper_status_text())

    elif cmd == "/fill":
        # Считаем медленно, поэтому сразу отправляем «считаю…» через
        # _api_call, чтобы получить message_id, потом редактируем это
        # же сообщение на результат.
        specs = _load_specs_safe()
        if args:
            table = args[0]
            sent = _api_call(
                "sendMessage", token, chat_id=chat_id,
                text=f"⏳ Считаю «{_html_escape(table)}»…",
                parse_mode="HTML")
            try:
                msg_id = sent["result"]["message_id"]
            except (TypeError, KeyError):
                msg_id = None
            body = _fill_table_detailed(table, specs)
            kb = {"inline_keyboard": [[
                {"text": "◀ ко всем таблицам",
                 "callback_data": "fill:refresh"}
            ]]}
            if msg_id:
                edit_message_text(token, chat_id, msg_id, body,
                                  reply_markup=kb)
            else:
                _reply(token, chat_id, body, reply_markup=kb)
        else:
            sent = _api_call(
                "sendMessage", token, chat_id=chat_id,
                text="⏳ Считаю заполненность по всем таблицам…",
                parse_mode="HTML")
            try:
                msg_id = sent["result"]["message_id"]
            except (TypeError, KeyError):
                msg_id = None
            body = _fill_summary_all(specs)
            kb = _fill_keyboard()
            if msg_id:
                edit_message_text(token, chat_id, msg_id, body,
                                  reply_markup=kb)
            else:
                _reply(token, chat_id, body, reply_markup=kb)

    elif cmd == "/html":
        # /html            — меню
        # /html history    — генерация и отправка history.html
        # /html completeness
        # /html dashboard  — только существующий файл
        # /html files      — список всех HTML
        if not args:
            _reply(token, chat_id, _html_menu_text(),
                   reply_markup=_html_menu_keyboard())
        else:
            sub = args[0].lower()
            if sub in ("files", "list"):
                _reply(token, chat_id, _html_list_files())
            elif sub in HTML_REPORTS:
                cfg = HTML_REPORTS[sub]
                if cfg["script"] is None:
                    if cfg["output"].exists():
                        send_document(token, chat_id, cfg["output"],
                                      caption=cfg["caption"])
                    else:
                        _reply(token, chat_id,
                               f"❌ <code>{cfg['output'].name}</code> "
                               f"ещё не создан. Запусти /run.")
                else:
                    sent = _api_call(
                        "sendMessage", token, chat_id=chat_id,
                        text=f"⏳ Генерирую "
                             f"<b>{_html_escape(cfg['title'])}</b>…",
                        parse_mode="HTML")
                    try:
                        msg_id = sent["result"]["message_id"]
                    except (TypeError, KeyError):
                        msg_id = None
                    ok, msg, path = _html_generate(sub)
                    if not ok:
                        if msg_id:
                            edit_message_text(token, chat_id, msg_id, msg)
                        else:
                            _reply(token, chat_id, msg)
                    else:
                        if msg_id:
                            edit_message_text(
                                token, chat_id, msg_id,
                                f"✅ Готово: "
                                f"<code>{_html_escape(path.name)}</code>")
                        send_document(token, chat_id, path,
                                      caption=cfg["caption"])
            else:
                _reply(token, chat_id,
                       f"Неизвестный отчёт «{_html_escape(sub)}». "
                       f"Доступные: history, completeness, dashboard, files.")

    elif cmd == "/files":
        if not REPORTS.exists():
            _reply(token, chat_id, "Папка reports/ не найдена.")
            return
        items = [p for p in REPORTS.iterdir()
                 if p.is_file() and not p.name.startswith("_")]
        items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        items = items[:15]
        if not items:
            _reply(token, chat_id, "В reports/ пока пусто.")
            return
        lines = ["<b>Свежие файлы в reports/</b>", ""]
        for p in items:
            sz = p.stat().st_size
            unit = "Б"
            if sz >= 1024 * 1024:
                sz, unit = sz // (1024 * 1024), "МБ"
            elif sz >= 1024:
                sz, unit = sz // 1024, "КБ"
            when = datetime.fromtimestamp(
                p.stat().st_mtime).strftime("%d.%m %H:%M")
            lines.append(f"• <code>{_html_escape(p.name)}</code> — "
                         f"{sz} {unit}, {when}")
        _reply(token, chat_id, "\n".join(lines))

    elif cmd == "/log":
        n = 30
        if args:
            try:
                n = max(1, min(int(args[0]), 200))
            except ValueError:
                pass
        if not BOT_LOG.exists():
            _reply(token, chat_id, "Лог tg_bot.log ещё не создан.")
            return
        try:
            tail = BOT_LOG.read_text(
                encoding="utf-8", errors="replace").splitlines()[-n:]
        except OSError as e:
            _reply(token, chat_id, f"Не смог прочитать лог: {e}")
            return
        if not tail:
            _reply(token, chat_id, "Лог пуст.")
            return
        tail = [(t[:200] + "…" if len(t) > 200 else t) for t in tail]
        _reply(token, chat_id,
               f"<b>Последние {len(tail)} строк лога:</b>\n"
               f"<pre>{_html_escape(chr(10).join(tail))}</pre>")

    elif cmd == "/invite":
        _role = "viewer"
        if args:
            _role = args[0].lower()
            if _role not in ("viewer", "admin"):
                _reply(token, chat_id,
                       "Формат: <code>/invite [viewer|admin]</code>")
                return
        _code, _expires = bot_users.create_invite(_role, chat_id)
        _url = (f"https://t.me/{_BOT_USERNAME}?start={_code}"
                if _BOT_USERNAME else f"/start {_code}")
        _reply(token, chat_id,
               f"🎟 <b>Приглашение создано</b>\n\n"
               f"Роль: <b>{_role}</b>\n"
               f"Код: <code>{_code}</code>\n"
               f"Срок: до {_expires[:16].replace('T', ' ')}\n"
               f"Одноразовый.\n\n"
               f"Отправьте ссылку человеку:\n"
               f"<code>{_url}</code>\n\n"
               f"Если ссылка не открывается — пусть напишет боту:\n"
               f"<code>/start {_code}</code>")

    elif cmd == "/invites":
        _invs = bot_users.list_invites()
        if not _invs:
            _reply(token, chat_id, "Активных приглашений нет.")
            return
        lines = ["<b>Приглашения</b>", ""]
        for inv in _invs[:30]:
            if inv["used_by"]:
                status = f"✅ использован ({inv['used_by']})"
            elif inv["expired"]:
                status = "⌛ истёк"
            else:
                status = "🟢 активен"
            lines.append(
                f"• <code>{inv['code']}</code> — "
                f"<b>{inv['role']}</b> — {status}")
        lines += ["", "Отозвать: <code>/revoke &lt;код&gt;</code>"]
        _reply(token, chat_id, "\n".join(lines))

    elif cmd == "/revoke":
        if not args:
            _reply(token, chat_id, "Формат: <code>/revoke &lt;код&gt;</code>")
            return
        ok, msg = bot_users.revoke_invite(args[0])
        _reply(token, chat_id, ("✅ " if ok else "❌ ") + msg)

    elif cmd == "/users":
        _users_text = "<b>Пользователи бота</b>\n\n"
        for u in bot_users.list_users():
            _users_text += (f"• <code>{_html_escape(u['chat_id'])}</code> — "
                            f"<b>{_html_escape(u['role'])}</b>"
                            + (f" ({_html_escape(u['name'])})" if u['name'] else "")
                            + f"\n  добавлен: {_html_escape(u['added_at'])}\n")
        _reply(token, chat_id, _users_text)

    elif cmd == "/pending":
        pending = bot_users.list_pending()
        if not pending:
            _reply(token, chat_id, "Ожидающих нет.")
            return
        lines = ["<b>Писали боту, но не в списке:</b>", ""]
        for p in pending[:30]:
            lines.append(
                f"• <code>{_html_escape(p['chat_id'])}</code>"
                + (f" {_html_escape(p['name'])}" if p["name"] else "")
                + f" — попыток {p['attempts']}, последнее: "
                  f"{_html_escape(p['last_text'][:60])}")
        lines += ["", "Добавить: <code>/add_user &lt;id&gt; [viewer|admin]</code>"]
        _reply(token, chat_id, "\n".join(lines))

    elif cmd == "/add_user":
        if not args:
            _reply(token, chat_id,
                   "Формат: <code>/add_user &lt;chat_id&gt; "
                   "[viewer|admin]</code>")
            return
        _cid = args[0]
        _role = args[1].lower() if len(args) > 1 else "viewer"
        ok, msg = bot_users.add_user(_cid, _role)
        _reply(token, chat_id, ("✅ " if ok else "❌ ") + msg)

    elif cmd == "/remove_user":
        if not args:
            _reply(token, chat_id,
                   "Формат: <code>/remove_user &lt;chat_id&gt;</code>")
            return
        ok, msg = bot_users.remove_user(args[0])
        _reply(token, chat_id, ("✅ " if ok else "❌ ") + msg)

    elif cmd == "/set_role":
        if len(args) < 2:
            _reply(token, chat_id,
                   "Формат: <code>/set_role &lt;chat_id&gt; "
                   "[viewer|admin]</code>")
            return
        ok, msg = bot_users.set_role(args[0], args[1].lower())
        _reply(token, chat_id, ("✅ " if ok else "❌ ") + msg)

    else:
        _reply(token, chat_id,
               f"Неизвестная команда: {cmd}. /help — что я умею.")


# ──────────────────────────────────────────────────────────────
#  Обработка inline-кнопок
# ──────────────────────────────────────────────────────────────
def _handle_settings_callback(token: str, chat_id: str, cb_id: str,
                              message_id, data: str) -> None:
    """Обрабатывает клики по /settings."""
    s = _load_settings()
    srcs = _list_known_sources()
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    page_offset = 0
    page_size = 10

    if action == "toggle_src":
        chosen = set(s.get("sources") or [])
        if arg in chosen:
            chosen.discard(arg)
        else:
            chosen.add(arg)
        s["sources"] = sorted(chosen)
        _save_settings(s)
        answer_callback(token, cb_id, "изменено")

    elif action == "toggle_opt":
        if arg in ("excel", "no_anomalies"):
            s[arg] = not bool(s.get(arg))
            _save_settings(s)
        answer_callback(token, cb_id, "изменено")

    elif action == "cycle_conc":
        cur = int(s.get("concurrency") or 0)
        try:
            idx = CONCURRENCY_CYCLE.index(cur)
        except ValueError:
            idx = 0
        s["concurrency"] = CONCURRENCY_CYCLE[
            (idx + 1) % len(CONCURRENCY_CYCLE)]
        _save_settings(s)
        answer_callback(token, cb_id, f"concurrency={s['concurrency']}")

    elif action == "cycle_sens":
        cur = float(s.get("sensitivity") or 0)
        try:
            idx = SENSITIVITY_CYCLE.index(cur)
        except ValueError:
            idx = 0
        s["sensitivity"] = SENSITIVITY_CYCLE[
            (idx + 1) % len(SENSITIVITY_CYCLE)]
        _save_settings(s)
        answer_callback(token, cb_id, f"sensitivity={s['sensitivity']}")

    elif action == "reset":
        s = dict(DEFAULT_SETTINGS)
        _save_settings(s)
        answer_callback(token, cb_id, "сброшено")

    elif action == "page":
        try:
            page_offset = max(0, int(arg))
        except ValueError:
            page_offset = 0
        answer_callback(token, cb_id)

    elif action == "run":
        answer_callback(token, cb_id, "запускаю…")
        args = _settings_to_args(s)
        ok, msg = _scraper_start(chat_id, token, args)
        try:
            edit_message_text(
                token, chat_id, message_id,
                _settings_text(s, srcs)
                + "\n\n" + ("▶️ запущено" if ok else "⚠️ не запущено"),
                reply_markup={"inline_keyboard": []})
        except Exception:
            pass
        _reply(token, chat_id, msg)
        return

    elif action == "noop":
        answer_callback(token, cb_id)
        return

    else:
        answer_callback(token, cb_id, "неизвестное действие")
        return

    edit_message_text(token, chat_id, message_id,
                      _settings_text(s, srcs),
                      reply_markup=_settings_keyboard(s, srcs,
                                                      offset=page_offset,
                                                      page=page_size))


def handle_callback(token: str, cb: dict) -> None:
    cb_id = cb.get("id") or ""
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    message_id = msg.get("message_id")

    # Быстро гасим часики в клиенте.
    answer_callback(token, cb_id)

    if not chat_id or not isinstance(message_id, int):
        return

    if data.startswith("jc:"):
        try:
            idx = int(data[3:])
        except ValueError:
            return
        if idx < 0 or idx >= len(_CARDS_CACHE):
            # Кнопка из старого сообщения, кэш уже не тот.
            answer_callback(token, cb_id,
                            "Кнопка устарела. Отправьте /top заново.")
            return
        c = _CARDS_CACHE[idx]
        new_text = _format_card_full(c)
        # Кнопка "◀ назад" — показать список заново.
        back_kb = {"inline_keyboard": [[
            {"text": "◀ назад к списку", "callback_data": "back"}
        ]]}
        edit_message_text(token, chat_id, message_id,
                          new_text, reply_markup=back_kb)
        LOG.info("callback jc:%d (%s) — карточка отправлена",
                 idx, c.get("jc", "?"))

    elif data.startswith("set:"):
        _handle_settings_callback(token, chat_id, cb_id,
                                  message_id, data)
        return

    elif data.startswith("html:"):
        _handle_html_callback(token, chat_id, cb_id,
                              message_id, data)
        return

    elif data.startswith("fill:"):
        _handle_fill_callback(token, chat_id, cb_id,
                              message_id, data)
        return

    elif data == "back":
        # Просто подсказка: список уже был выше.
        answer_callback(token, cb_id,
                        "Список — в сообщении выше. /top или /find заново.")
        LOG.info("callback back")

    else:
        LOG.warning("неизвестный callback: %r", data[:60])


# ──────────────────────────────────────────────────────────────
#  Long polling
# ──────────────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"offset": 0}


def _save_state(state: dict) -> None:
    try:
        STATE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def poll_loop(token: str, allowed_chats: set) -> None:
    state = _load_state()
    offset = int(state.get("offset") or 0)
    me = _api_call("getMe", token)
    if not me:
        print("❌ Токен не принят Telegram.")
        LOG.error("getMe отклонил токен")
        return
    username = me["result"].get("username") or "?"
    global _BOT_USERNAME
    _BOT_USERNAME = username
    print(f"🤖 Бот @{username} запущен (long polling).")
    # Роли для тех, кто из окружения.
    # chat_id в poll_loop появляется только внутри цикла обработки
    # сообщений. Здесь берём его из окружения напрямую.
    #
    # TELEGRAM_CHAT_ID     → admin (владелец)
    # TELEGRAM_EXTRA_CHATS → viewer (гости, только чтение)
    _main_chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if bot_users is not None and _main_chat:
        if bot_users.get_role(_main_chat) is None:
            bot_users.ensure_admin(_main_chat)
            LOG.info("TELEGRAM_CHAT_ID добавлен как admin: %s",
                     _main_chat)
    # Гости из extra — viewer, если их ещё нет.
    _extra_raw = (os.environ.get("TELEGRAM_EXTRA_CHATS") or "").strip()
    if bot_users is not None and _extra_raw:
        for _cid in [x.strip() for x in _extra_raw.split(",") if x.strip()]:
            if _cid == _main_chat:
                continue
            if bot_users.get_role(_cid) is None:
                bot_users.add_user(_cid, "viewer")
                LOG.info("TELEGRAM_EXTRA_CHATS: %s → viewer", _cid)
    log_startup_environment()
    LOG.info("Бот @%s запущен", username)

    # Уведомление о запуске + устанавливаем клавиатуру снизу.
    try:
        msg = (f"🤖 <b>Бот @{_html_escape(username)} запущен</b>\n"
               f"Время: {BOT_STARTED_AT:%d.%m.%Y %H:%M:%S}\n"
               f"Снизу — кнопки основных команд.")
        for cid in allowed_chats:
            send_message(token, cid, msg, parse_mode="HTML",
                         reply_markup=main_keyboard(
                             bot_users.get_role(cid) or "viewer"
                             if bot_users else "viewer"))
    except Exception as e:
        LOG.warning("не удалось отправить уведомление о старте: %s", e)

    while True:
        try:
            r = requests.get(
                f"{API}/bot{token}/getUpdates",
                params={"offset": offset, "timeout": POLL_TIMEOUT,
                        "allowed_updates": json.dumps(
                            ["message", "edited_message", "callback_query"])},
                timeout=POLL_TIMEOUT + 10)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            LOG.warning("getUpdates: %s: %s", type(e).__name__, e)
            time.sleep(3)
            continue
        if not data.get("ok"):
            LOG.warning("getUpdates: %s", data.get("description"))
            time.sleep(3)
            continue

        for upd in data.get("result") or []:
            offset = upd["update_id"] + 1
            state["offset"] = offset
            _save_state(state)

            # 1. Inline-кнопки.
            cb = upd.get("callback_query")
            if cb:
                chat = (cb.get("message") or {}).get("chat") or {}
                cb_chat_id = str(chat.get("id") or "")
                if allowed_chats and cb_chat_id not in allowed_chats:
                    LOG.warning("callback от %s — не разрешён", cb_chat_id)
                    continue
                try:
                    handle_callback(token, cb)
                except Exception as e:
                    LOG.exception("ошибка обработки callback: %s", e)
                continue

            # 2. Обычные сообщения.
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            text = msg.get("text") or ""
            if not chat_id or not text:
                continue
            if allowed_chats and chat_id not in allowed_chats:
                LOG.warning("сообщение от %s — не в списке разрешённых",
                            chat_id)
                continue
            try:
                handle_command(token, chat_id, text,
                               role=(bot_users.get_role(chat_id)
                                     if bot_users else "viewer"))
            except Exception as e:
                LOG.exception("ошибка обработки команды: %s", e)


# ──────────────────────────────────────────────────────────────
#  Watch
# ──────────────────────────────────────────────────────────────
def watch_loop(token: str, chat_ids: list, baseline_ts: float) -> None:
    LOG.info("Следим за %s (каждые %d сек).", REPORTS, WATCH_INTERVAL)
    seen: set = set()
    while True:
        try:
            for md in REPORTS.glob("problem_jc_*.md"):
                if md.name in seen:
                    continue
                try:
                    mtime = md.stat().st_mtime
                except OSError:
                    continue
                if mtime <= baseline_ts:
                    seen.add(md.name)
                    continue
                seen.add(md.name)
                LOG.info("новый отчёт: %s", md.name)
                caption = build_report_caption(md)
                for cid in chat_ids:
                    send_document(token, cid, md, caption=caption)
                    csv = md.with_suffix(".csv")
                    if csv.exists():
                        send_document(token, cid, csv,
                                      caption="Проблемы (CSV)")
        except Exception as e:
            LOG.warning("watch: %s: %s", type(e).__name__, e)
        time.sleep(WATCH_INTERVAL)


# ──────────────────────────────────────────────────────────────
#  main
# ──────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram-бот для проблемных ЖК")
    ap.add_argument("--send-only", metavar="TEXT")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--chat", default=None)
    ap.add_argument("--token", default=None)
    args = ap.parse_args()

    token, chat_id, extra = _get_conf()
    if args.token:
        token = args.token.strip()
    if args.chat:
        chat_id = args.chat.strip()

    if not token:
        print("❌ TELEGRAM_BOT_TOKEN не задан в .env или через --token.")
        return 1

    if args.send_only:
        if not chat_id:
            print("❌ TELEGRAM_CHAT_ID не задан.")
            return 1
        ok = send_message(token, chat_id, args.send_only, parse_mode=None)
        print("✅ отправлено" if ok else "❌ не отправлено")
        return 0 if ok else 1

    if not chat_id:
        print("❌ TELEGRAM_CHAT_ID не задан.")
        return 1

    allowed = {chat_id} | set(extra)
    baseline = time.time() - WATCH_BASELINE_MTIME_OFFSET

    if args.watch:
        threading.Thread(target=watch_loop,
                         args=(token, list(allowed), baseline),
                         daemon=True).start()

    try:
        poll_loop(token, allowed)
    except KeyboardInterrupt:
        print("\nОстановлено.")
        LOG.info("Остановлено пользователем (Ctrl+C)")
    return 0


if __name__ == "__main__":
    sys.exit(main())