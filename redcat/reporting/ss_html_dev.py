"""ss_html_dev — блоки застройщиков в HTML-дашборде."""
from __future__ import annotations

from redcat.web import webapp
from redcat.reporting.ss_config import DELTA_OK, DELTA_WARN
from redcat.reporting.ss_html_assets import _DEV_FILTER_JS
from redcat.reporting.ss_html_parts import _esc
from redcat.reporting.ss_metrics import _class_for_pct


def _relevant_developers(st: dict, specs: dict | None = None) -> set:
    """Застройщики, которые имеют отношение к источнику st["source"].

    Логика:
      1. Из cross_check.filter_right источника — фильтр по developer_name.
         Если там developer_name = "ГК А101", значит «свои» — все
         застройщики, содержащие эту подстроку.
      2. Плюс все, у кого есть хоть один сопоставленный ЖК (ok/warn/
         critical/insufficient) — эти застройщики точно участвуют
         в сверке.
    """
    rel: set = set()
    src_key = st.get("source")
    if not src_key:
        return rel

    # 1) Из filter_right.
    try:
        if specs is None:
            specs = webapp.load_specs()
        spec = specs.get(src_key)
        cc = getattr(spec, "cross_check", None) or {}
        filters = cc.get("filter_right") or []
        if isinstance(filters, dict):
            filters = [filters]
        wanted: list = []
        for f in filters:
            if not isinstance(f, dict):
                continue
            fld = str(f.get("field") or "")
            if "developer" not in fld.lower():
                continue
            val = str(f.get("value") or "").strip()
            op = str(f.get("op") or "eq").lower()
            if val:
                wanted.append((op, val))

        if wanted:
            for d in st.get("by_developer") or []:
                dev = d.get("developer") or ""
                for op, val in wanted:
                    ok = False
                    if op in ("eq", "="):
                        ok = (dev.strip() == val.strip())
                    elif op in ("contains", "starts", "ends", "like"):
                        ok = (val.lower() in dev.lower())
                    elif op in ("in", "notin"):
                        vals = [x.strip() for x in val.split(",")]
                        ok = (dev.strip() in vals)
                    if ok:
                        rel.add(dev)
                        break
    except Exception:
        pass

    # 2) Все, у кого есть сопоставленные ЖК.
    for d in st.get("by_developer") or []:
        matched = (int(d.get("ok") or 0)
                   + int(d.get("warn") or 0)
                   + int(d.get("critical") or 0)
                   + int(d.get("insufficient") or 0))
        if matched > 0:
            rel.add(d.get("developer") or "")
    rel.discard("")
    return rel


def _developer_block(st: dict, relevant: set | None = None) -> str:
    """Свёрнутая секция «по застройщикам».

    relevant — набор «своих» застройщиков. Если не пусто, в селекте
    появится опция «— только относящиеся —», и она будет выбрана
    по умолчанию.
    """
    devs = st.get("by_developer") or []
    if not devs:
        return ""

    relevant = relevant or set()

    metric_names = list((st.get("metrics_pairs") or {}).keys())
    n_devs = len(devs)
    problem = sum(1 for d in devs if d["critical"] or d["warn"])

    def _worst(item):
        worst = 0.0
        for m in metric_names:
            mm = (item.get("metrics") or {}).get(m) or {}
            if mm.get("diff_pct") is not None:
                worst = max(worst, abs(mm["diff_pct"]))
        return worst

    head_metric_cols = "".join(f"<th>{_esc(m)} Δ%</th>" for m in metric_names)

    # Порядок: сначала «свои», потом остальные.
    def sort_key(d):
        is_rel = 0 if (d.get("developer") in relevant) else 1
        return (is_rel, -d["critical"], -d["warn"],
                -(d["worst_pct"] or 0), -d["jc_count"])
    ordered = sorted(devs, key=sort_key)

    # Опции селекта — в том же порядке.
    options_html = ['<option value="">— всех —</option>']
    if relevant:
        options_html.append(
            f'<option value="__relevant__" selected>'
            f'— только относящиеся ({len([d for d in devs if d.get("developer") in relevant])}) —'
            f'</option>'
        )
    for d in ordered:
        mark = "★ " if d.get("developer") in relevant else ""
        options_html.append(
            f'<option value="{_esc(d["developer"])}">'
            f'{mark}{_esc(d["developer"])} '
            f'(руками {d["critical"]}, вним. {d["warn"]}, '
            f'ok {d["ok"]}, RC {d["left_only"]}, всего {d["jc_count"]})'
            f'</option>'
        )
    options = "".join(options_html)

    details_html = []
    for d in ordered:
        pills = []
        if d["critical"]:
            pills.append(f'<span class="pill crit">руками {d["critical"]}</span>')
        if d["warn"]:
            pills.append(f'<span class="pill warn">вним. {d["warn"]}</span>')
        if d["ok"]:
            pills.append(f'<span class="pill ok">ok {d["ok"]}</span>')
        if d["insufficient"]:
            pills.append(f'<span class="pill info">мало {d["insufficient"]}</span>')
        if d.get("left_only"):
            pills.append(f'<span class="pill gray">только RC {d["left_only"]}</span>')
        if d.get("right_only"):
            pills.append(f'<span class="pill gray">только ист. {d["right_only"]}</span>')
        pills_html = " ".join(pills)

        m_summary = " · ".join(
            f'{_esc(m)}: медиана {v["median_pct"]:+.1f}%'
            for m, v in d["metrics"].items()
        ) or "—"

        jc_rows = []
        for it in sorted(d["items"], key=_worst, reverse=True):
            cells = []
            for m in metric_names:
                mm = (it.get("metrics") or {}).get(m) or {}
                pct = mm.get("diff_pct")
                if pct is None:
                    cells.append('<td class="num">—</td>')
                else:
                    cls = _class_for_pct(pct)
                    cells.append(
                        f'<td class="num"><span class="pill {cls}">'
                        f'{pct:+.1f}%</span></td>')
            jc_rows.append(
                f'<tr><td>{_esc(it.get("display"))}</td>'
                f'<td class="num">{it.get("left_rows", 0)}</td>'
                f'<td class="num">{it.get("right_rows", 0)}</td>'
                f'{"".join(cells)}</tr>')

        is_rel = "1" if d.get("developer") in relevant else "0"
        details_html.append(f'''
        <details class="dev-row" data-dev="{_esc(d["developer"])}" data-dev-rel="{is_rel}">
          <summary>
            <b>{_esc(d["developer"])}</b>
            <span class="muted">· ЖК: {d["jc_count"]}</span>
            <span class="pills">{pills_html}</span>
            <span class="msummary">{m_summary}</span>
          </summary>
          <div class="scroll" style="max-height:400px">
            <table>
              <thead><tr><th>ЖК</th><th>лотов слева</th>
              <th>лотов справа</th>{head_metric_cols}</tr></thead>
              <tbody>{"".join(jc_rows)}</tbody>
            </table>
          </div>
        </details>''')

    rel_note = ""
    if relevant:
        rel_note = (f'<div class="muted" style="margin-top:6px">'
                    f'Относящихся к этому источнику застройщиков: '
                    f'<b>{len(relevant)}</b>. Они отмечены ★ и показаны '
                    f'по умолчанию.</div>')

    return f'''
    <div class="card dev-card">
      <h3>По застройщикам
        <span class="muted" style="font-weight:400">
          — {n_devs} застройщиков, из них с проблемами: {problem}
        </span>
      </h3>
      <div class="row" style="margin:8px 0 6px">
        <label class="inline">показать только:
          <select data-dev-filter onchange="devFilter(this)"
                  style="padding:3px 8px">
            {options}
          </select>
        </label>
      </div>
      {rel_note}
      <div class="muted" style="margin:4px 0 10px">
        Одна строка — застройщик; кликните, чтобы раскрыть список его ЖК
        и увидеть, у каких именно расхождение. Пороги Δ%: ok ≤ {DELTA_OK:g}%,
        вним. ≤ {DELTA_WARN:g}%, выше — «руками».
      </div>
      <div class="dev-list">{"".join(details_html)}</div>
    </div>
    {_DEV_FILTER_JS}'''
