"""do_sql — безопасное выполнение SELECT-запросов пользователя."""
from __future__ import annotations

import re
import sqlite3
from redcat.data.do_core import DataError, SQL_ROW_LIMIT, connect_ro


# ──────────────────────────────────────────────────────────────
#  SQL-КОНСОЛЬ (строго SELECT)
# ──────────────────────────────────────────────────────────────
# Опасные команды. Ищем их как команды, а не как часть слова, иначе
# REPLACE(...) и sales_analyze ловятся как запрещённые.
# Команда должна стоять в начале утверждения, после ; или после (.
_FORBIDDEN_STMT = re.compile(
    r"(?im)(?:^|;|\()\s*("
    r"insert|update|delete|drop|alter|create|attach|detach|"
    r"vacuum|reindex|pragma|begin|commit|rollback|savepoint|release"
    r")\b"
)


# Модификатор INSERT OR REPLACE.
_FORBIDDEN_KEYWORD = re.compile(r"(?i)\breplace\s+into\b")


def _is_safe_sql(text: str) -> bool:
    """True — запрос безопасен (чтение). False — есть запрещённое."""
    stripped = re.sub(r"'[^']*'", "''", text)
    if _FORBIDDEN_STMT.search(stripped):
        return False
    if _FORBIDDEN_KEYWORD.search(stripped):
        return False
    return True


def run_sql(db_path, sql, limit=SQL_ROW_LIMIT) -> dict:
    """Выполняет пользовательский SELECT. Всё остальное отклоняется.

    Защит три, и они независимы: соединение открыто только на чтение,
    разрешено единственное выражение, и оно обязано начинаться с SELECT
    или WITH. Даже при обходе одной проверки остальные держат.
    """
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise DataError("Пустой запрос.")
    if ";" in text:
        raise DataError("Можно выполнить только один запрос за раз "
                        "(точка с запятой внутри не допускается).")
    if not re.match(r"^(select|with)\b", text, re.IGNORECASE):
        raise DataError("Разрешены только запросы SELECT / WITH — "
                        "приложение не изменяет собранные данные.")
    if not _is_safe_sql(text):
        raise DataError("В запросе есть изменяющая команда. "
                        "Доступно только чтение.")

    with connect_ro(db_path) as conn:
        try:
            cur = conn.execute(text)
            rows = cur.fetchmany(limit)
            cols = [d[0] for d in cur.description] if cur.description else []
        except sqlite3.Error as e:
            raise DataError(f"SQLite: {e}") from e
    return {"columns": cols, "rows": [dict(r) for r in rows],
            "truncated": len(rows) >= limit, "limit": limit}
