"""ex_format — форматирование чисел, процентов и классов расхождений."""
from __future__ import annotations

import re
from redcat.reporting.ex_config import SOURCE_TO_DEVELOPER
from redcat.reporting.ex_texts import METRIC_LABELS


# ── утилиты ───────────────────────────────────────────────────
def _to_num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(" ", "").replace("\xa0", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _clean_title(t: str) -> str:
    if not t:
        return ""
    return re.split(r"\s+[—–-]\s+|\(|\[|:", t, maxsplit=1)[0].strip()


def developer_for(source_key: str, specs: dict) -> str:
    if source_key in SOURCE_TO_DEVELOPER:
        return SOURCE_TO_DEVELOPER[source_key]
    spec = specs.get(source_key)
    if spec and getattr(spec, "title", ""):
        c = _clean_title(spec.title)
        if c:
            return c
    return source_key


def metric_label(name: str) -> str:
    return METRIC_LABELS.get(name, name)


def _fmt_val(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}".replace(".", ",")
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    return str(v)


def _fmt_pct(p) -> str:
    if p is None:
        return ""
    return f"{p:+.1f}%".replace(".", ",")


def _fmt_abs(a) -> str:
    if a is None:
        return ""
    if isinstance(a, float):
        return f"{a:+.2f}".replace(".", ",")
    if isinstance(a, int):
        return f"{a:+,}".replace(",", " ")
    return str(a)


def _class_for_pct(pct, threshold):
    if pct is None:
        return "ok"
    a = abs(pct)
    if a < threshold:
        return "ok"
    if a < 5:
        return "small"
    if a < 10:
        return "medium"
    return "crit"


def _class_label(cls: str) -> str:
    return {"ok": "ok", "small": "мелкое",
            "medium": "среднее", "crit": "крупное"}.get(cls, cls)


def _md_icon(cls: str) -> str:
    return {"crit": "🔴", "medium": "⚠️", "small": "·", "ok": "✅"}.get(cls, "")
