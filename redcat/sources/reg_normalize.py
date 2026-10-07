"""reg_normalize — разворачивание, вычисляемые поля и нормализация записей."""
from __future__ import annotations

import json
import logging
import re
from redcat.sources.reg_core import SourceSpec


# ──────────────────────────────────────────────────────────────
#  НОРМАЛИЗАЦИЯ
# ──────────────────────────────────────────────────────────────
def dig(obj, path):
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


def _stringify_list(items):
    parts = []
    for it in items:
        if isinstance(it, dict):
            label = it.get("name") or it.get("title") or it.get("id")
            extra = it.get("walk_time") or it.get("distance")
            parts.append(f"{label} ({extra})" if label and extra else str(label))
        else:
            parts.append(str(it))
    return "; ".join(p for p in parts if p and p != "None")


def flatten_record(obj, prefix="", depth=0, max_depth=3) -> dict:
    flat = {}
    if not isinstance(obj, dict):
        return {prefix or "value": obj}

    for key, value in obj.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            if depth < max_depth:
                flat.update(flatten_record(value, name, depth + 1, max_depth))
            else:
                flat[name] = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, list):
            flat[name] = _stringify_list(value)
        else:
            flat[name] = value
    return flat


_SAFE_EXPR = re.compile(r"^[\w\s.+\-*/()]+$")


def _eval_derived(expr: str, row: dict):
    if not _SAFE_EXPR.match(expr):
        raise ValueError(f"Недопустимое выражение: {expr!r}")
    if "*" in expr and "**" in expr.replace(" ", ""):
        raise ValueError(
            f"Оператор ** запрещён — вычисление слишком дорогое: {expr!r}")
    scope = {}
    for k, v in row.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            scope[k.replace(".", "_")] = v
    try:
        return eval(expr.replace(".", "_"), {"__builtins__": {}}, scope)  # noqa: S307
    except (NameError, TypeError, ZeroDivisionError, SyntaxError):
        return None


def normalize(records, spec: SourceSpec) -> list:
    rows = []
    for item in records:
        flat = flatten_record(item, max_depth=spec.flatten_depth)

        if spec.mapping:
            row = {}
            for out_col, path in spec.mapping.items():
                if isinstance(path, str) and path in flat:
                    row[out_col] = flat[path]
                else:
                    parts = path.split(".") if isinstance(path, str) else list(path)
                    value = dig(item, parts)
                    row[out_col] = (_stringify_list(value)
                                    if isinstance(value, list) else value)
        else:
            row = flat

        for name, expr in spec.derived.items():
            try:
                row[name] = _eval_derived(expr, row)
            except ValueError as e:
                logging.warning("Источник '%s': %s", spec.key, e)
                row[name] = None

        rows.append(row)
    return rows
