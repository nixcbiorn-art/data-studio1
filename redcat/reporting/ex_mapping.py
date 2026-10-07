"""ex_mapping — определение типа цены и проверка маппинга метрик."""
from __future__ import annotations

from redcat.reporting.ex_config import _BASE, _DISCOUNTED
from redcat.reporting.ex_texts import DIAG, _KIND_LABEL


def _price_kind(name: str) -> str:
    n = (name or "").lower()
    if any(h in n for h in _DISCOUNTED):
        return "discounted"
    if any(h in n for h in _BASE):
        return "base"
    return "unknown"


def _is_price(name: str) -> bool:
    n = (name or "").lower()
    if not n: return False
    if any(h in n for h in ("price", "cost", "цена", "стоимост")):
        if any(x in n for x in ("per_sqm", "per_m2", "за м", "за_м", "meter", "sqm", "m2")):
            return False
        return True
    return False


def _price_pairs(metrics: dict) -> list[tuple]:
    out = []
    for lf, rf in (metrics or {}).items():
        if _is_price(lf) or _is_price(rf):
            out.append((lf, rf, _price_kind(lf), _price_kind(rf)))
    return out


def check_mapping(source_key: str, specs: dict) -> list[str]:
    spec = specs.get(source_key)
    cc = getattr(spec, "cross_check", None) or {}
    metrics = cc.get("metrics") or {}
    warns = []
    for lf, rf, lk, rk in _price_pairs(metrics):
        if lk == "unknown" or rk == "unknown" or lk == rk:
            continue
        warns.append(DIAG["map_mismatch"].format(
            left=lf, right=rf, lk=_KIND_LABEL[lk], rk=_KIND_LABEL[rk]))
    return warns
