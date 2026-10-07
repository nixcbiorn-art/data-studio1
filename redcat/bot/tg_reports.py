"""tg_reports — разбор отчётов, карточки ЖК, diff, запуск экспорта, тексты статуса/топа."""
from __future__ import annotations

from redcat.core import runner
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from redcat.bot.tg_api import _html_escape
from redcat.bot.tg_common import BOT_STARTED_AT, EXPORT_LOG, EXPORT_SCRIPT, HERE, LOG, REPORTS

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
            runner.script_cmd("export_problem_jc.py"),
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