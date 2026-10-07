"""ss_collect — получение отчёта сверки по источнику (Redcat слева)."""
from __future__ import annotations

from redcat.web import webapp
from redcat.reporting.ss_developers import _attach_developers


# ──────────────────────────────────────────────────────────────
#  Транспонирование отчёта: Redcat — слева, источник — справа
# ──────────────────────────────────────────────────────────────
def swap_report(r: dict) -> dict:
    """Меняет стороны отчёта местами: то, что было справа, становится слева.

    Знак Δ% меняется (было «левое минус правое», станет «правое минус левое»),
    чтобы Δ% по-прежнему означал «источник минус Redcat» уже в новой
    ориентации.

    ВАЖНО: ключи внутри item["metrics"] тоже переназываются. В отчёте
    webapp._cross_check_report они записаны именами ЛЕВЫХ (внешних) полей,
    а после swap стороны меняются — теперь слева Redcat, и метрики должны
    называться его именами. Иначе блоки «по метрике», «по застройщикам»
    и «системный сдвиг» не находят значения и показывают «—».
    """
    # {внешнее_поле: redcat_поле} — карта переименования ключей в items
    original_metrics = r.get("metrics") or {}

    def _swap_metric(m: dict) -> dict:
        return {
            "left": m.get("right"), "left_n": m.get("right_n"),
            "right": m.get("left"), "right_n": m.get("left_n"),
            "diff_abs": (-m["diff_abs"] if m.get("diff_abs") is not None else None),
            "diff_pct": (-m["diff_pct"] if m.get("diff_pct") is not None else None),
        }

    def _swap_item(it: dict) -> dict:
        old_m = it.get("metrics") or {}
        new_m = {
            original_metrics.get(k, k): _swap_metric(v)
            for k, v in old_m.items()
        }
        return {
            **it,
            "left_rows": it.get("right_rows", 0),
            "right_rows": it.get("left_rows", 0),
            "metrics": new_m,
        }

    src_items = r.get("items_by_class") or {}
    new_items = {}
    for cls in ("critical", "warn", "ok", "insufficient"):
        new_items[cls] = [_swap_item(it) for it in (src_items.get(cls) or [])]
    new_items["left_only"] = [_swap_item(it)
                              for it in (src_items.get("right_only") or [])]
    new_items["right_only"] = [_swap_item(it)
                               for it in (src_items.get("left_only") or [])]

    s = r.get("summary") or {}
    new_summary = {**s,
                   "left_only": s.get("right_only", 0),
                   "right_only": s.get("left_only", 0)}

    return {
        **r,
        "source": r.get("with_table"),
        "with_table": r.get("source"),
        "on_left": r.get("on_right"),
        "on_right": r.get("on_left"),
        "left_label": r.get("right_label"),
        "right_label": r.get("left_label"),
        "left_external": r.get("right_external"),
        "right_external": r.get("left_external"),
        # верхнеуровневые metrics — тоже переворачиваются; их ключи
        # (Redcat'овские имена полей) должны совпасть с ключами в items
        "metrics": {v: k for k, v in original_metrics.items()},
        "summary": new_summary,
        "items_by_class": new_items,
    }


# ──────────────────────────────────────────────────────────────
#  Сбор
# ──────────────────────────────────────────────────────────────
def all_sources_with_cross_check() -> list[str]:
    specs = webapp.load_specs()
    return sorted(k for k, s in specs.items()
                  if getattr(s, "cross_check", None))


def collect(source_key: str) -> dict | None:
    """Возвращает отчёт по источнику (уже с Redcat слева) или None."""
    try:
        r = webapp._cross_check_report(source_key)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  {source_key}: {type(e).__name__}: {e}")
        return None
    r = swap_report(r)
    _attach_developers(r)
    return r
