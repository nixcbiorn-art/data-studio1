"""tg_fill — отчёты о заполненности таблиц (/fill)."""
from __future__ import annotations

from redcat.core import paths
import sys
from pathlib import Path
from redcat.bot.tg_api import _html_escape, answer_callback, edit_message_text
from redcat.bot.tg_common import HERE, LOG, REPORTS

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
        from redcat.sources import registry as _src
        _src.load_from_dir(paths.SOURCES_DIR)
        _src.load_from_dir(paths.SOURCES_EXT_DIR)
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
        from redcat.quality import completeness as _cm
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
        from redcat.quality import completeness as _cm
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