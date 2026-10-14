"""do_formula — проверка и вычисление формул над строками."""
from __future__ import annotations

import re
from redcat.data.do_core import DataError


# ──────────────────────────────────────────────────────────────
#  ВЫЧИСЛЯЕМЫЕ КОЛОНКИ (безопасная формула)
# ──────────────────────────────────────────────────────────────
_SAFE_EXPR = re.compile(r"^[\w\s.+\-*/()%<>=!]+$")


_DOUBLE_STAR = re.compile(r"\*\s*\*")


def validate_formula(expr: str, column_names) -> list:
    """Проверяет формулу до сохранения: символы, синтаксис, знакомые имена.

    Отдельно от eval_formula, потому что там проверить нельзя: на пустой
    строке любая формула честно падает на «нет такого имени», хотя сама
    формула правильная.
    """
    if _DOUBLE_STAR.search(expr or ""):
        raise DataError(
            "Оператор ** запрещён — вычисление слишком дорогое.")
    if not _SAFE_EXPR.match(expr or ""):
        raise DataError("В формуле есть недопустимые символы. Разрешены имена "
                        "колонок, числа и знаки + - * / ( ) % > < =")
    normalized = re.sub(r"\b([A-Za-z_]\w*)\.(\w+)", r"\1_\2", expr)
    try:
        compile(normalized, "<формула>", "eval")
    except SyntaxError as e:
        raise DataError(f"Синтаксическая ошибка в формуле: {e.msg}") from e
    known = {re.sub(r"\W", "_", c) for c in column_names}
    used = set(re.findall(r"[A-Za-z_]\w*", normalized))
    unknown = sorted(used - known)
    if unknown:
        raise DataError(f"В формуле нет таких колонок: {', '.join(unknown)}")
    return sorted(used & known)


def eval_formula(expr: str, row: dict):
    """Считает формулу вида `price / total_area` по значениям строки.

    Разрешены только имена колонок, числа и арифметика: никаких вызовов
    функций, импортов и доступа к атрибутам. Поэтому формулу безопасно
    принимать из браузера.
    """
    if _DOUBLE_STAR.search(expr or ""):
        raise DataError(
            "Оператор ** запрещён — вычисление слишком дорогое.")
    if not _SAFE_EXPR.match(expr or ""):
        raise DataError("В формуле есть недопустимые символы. Разрешены имена "
                        "колонок, числа и знаки + - * / ( ) % > < =")
    scope = {}
    for key, value in row.items():
        name = re.sub(r"\W", "_", key)
        try:
            scope[name] = float(str(value).replace("\xa0", "").replace(" ", "")
                                .replace(",", ".")) if value not in (None, "") else None
        except (TypeError, ValueError):
            scope[name] = None
    safe = re.sub(r"[^\w\s.+\-*/()%<>=!]", "", expr)
    safe = re.sub(r"\b([A-Za-z_]\w*)\.(\w+)", r"\1_\2", safe)
    try:
        return eval(safe, {"__builtins__": {}}, scope)  # noqa: S307 — выражение отфильтровано выше
    except ZeroDivisionError:
        return None
    except (NameError, TypeError, SyntaxError) as e:
        raise DataError(f"Формула не посчиталась: {e}") from e
