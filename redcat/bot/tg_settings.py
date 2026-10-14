"""tg_settings — настройки запуска сбора: хранение, текст и клавиатура."""
from __future__ import annotations

from redcat.core import paths
import json
from redcat.bot.tg_api import _html_escape
from redcat.bot.tg_common import HERE, LOG

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
    for folder, is_ext in ((paths.SOURCES_DIR, False),
                           (paths.SOURCES_EXT_DIR, True)):
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