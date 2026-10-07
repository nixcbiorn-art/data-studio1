"""web_routes_csv — пакетная загрузка CSV."""
from __future__ import annotations

import sqlite3
from redcat.data import dataops
from redcat.web.web_config import DATA_DB, EXTERNAL_DB
from redcat.web.web_csv import _CSV_UPLOAD_STATE, _csv_convert, _csv_dedupe_idents, _csv_pad_row, _csv_sniff_type
from redcat.web.web_data import load_specs


class CsvRoutes:
    """Пакетная загрузка CSV."""

    def _upload_csv_batch(self, b) -> dict:
        """Принимает один кусок («пачку») строк из CSV, который клиент шлёт
        отдельным POST-запросом. Файл целиком через веб-сервер на голом
        stdlib не пропустить с комфортом (да и не нужно) — вместо этого
        клиент режет данные на пачки по ~1000 строк и шлёт их по очереди,
        сервер копит их во временной таблице и на последней пачке
        достраивает настоящую таблицу с уже подобранными типами колонок.

        Таблица всегда пишется в external_data.db — «свои» данные логически
        такие же внешние, как fsk_apartments и подобные, и сразу попадают
        в приложение (вкладки «Данные», «Внешние», аналитика) без правки
        кода: список таблиц строится по факту наличия в базе, а не по
        сборщику.
        """
        table_raw = str(b.get("table") or "").strip()
        if not table_raw:
            raise dataops.DataError("Не указано имя таблицы.")
        table = dataops._safe_ident(table_raw)
        if table == "comparison_vs_previous" or table in load_specs():
            raise dataops.DataError(
                f"Имя «{table}» занято источником сбора — выберите другое.")

        seq = int(b.get("seq", 0))
        total = int(b.get("total", 1))
        mode = b.get("mode") if b.get("mode") in ("append", "replace") else "append"
        rows_in = b.get("rows") or []
        tmp_table = f"_csv_upload_tmp_{table}"

        with sqlite3.connect(str(EXTERNAL_DB), timeout=15) as conn:
            conn.execute("PRAGMA journal_mode=WAL")

            if seq == 0:
                if DATA_DB.exists() and table in {
                        t["name"] for t in dataops.list_tables(DATA_DB)}:
                    raise dataops.DataError(
                        f"Таблица «{table}» уже существует как источник сбора "
                        f"Redcat — выберите другое имя.")
                columns = b.get("columns") or []
                if not columns:
                    raise dataops.DataError(
                        "Не переданы названия колонок (columns) — они нужны "
                        "в самой первой пачке (seq=0).")
                columns = _csv_dedupe_idents(columns)
                conn.execute(f'DROP TABLE IF EXISTS "{tmp_table}"')
                cols_sql = ", ".join(f'"{c}" TEXT' for c in columns)
                conn.execute(f'CREATE TABLE "{tmp_table}" ({cols_sql})')
                _CSV_UPLOAD_STATE[table] = {"columns": columns, "inserted": 0}

            state = _CSV_UPLOAD_STATE.get(table)
            if not state:
                raise dataops.DataError(
                    "Загрузка не была начата (или прервалась) — обновите "
                    "страницу и загрузите файл заново с первой пачки.")
            columns = state["columns"]

            if rows_in:
                placeholders = ", ".join("?" for _ in columns)
                col_sql = ", ".join(f'"{c}"' for c in columns)
                sql = f'INSERT INTO "{tmp_table}" ({col_sql}) VALUES ({placeholders})'
                for chunk in dataops._chunks(rows_in, 500):
                    conn.executemany(
                        sql, [_csv_pad_row(r, len(columns)) for r in chunk])
                state["inserted"] += len(rows_in)
            conn.commit()

            finished = seq >= total - 1
            if finished:
                self._finalize_csv_upload(conn, table, tmp_table, columns, mode)
                _CSV_UPLOAD_STATE.pop(table, None)

            inserted_so_far = state["inserted"]

        return {"ok": True, "table": table, "seq": seq, "total": total,
                "inserted_so_far": inserted_so_far, "finished": finished}

    def _finalize_csv_upload(self, conn, table, tmp_table, columns, mode) -> None:
        """Последний шаг загрузки: подбирает тип каждой колонки по всем
        загруженным значениям, создаёт (или дополняет, в режиме «добавить»)
        настоящую таблицу и переносит данные с приведением типов, а не
        текстом как есть — иначе числовые колонки не сортировались бы как
        числа и не участвовали в аналитике (выбросы, распределения)."""
        cur = conn.cursor()
        col_sql_tmp = ", ".join(f'"{c}"' for c in columns)
        cur.execute(f'SELECT {col_sql_tmp} FROM "{tmp_table}"')
        all_rows = cur.fetchall()

        types = []
        for i in range(len(columns)):
            sample = [r[i] for r in all_rows[:500] if r[i] not in (None, "")]
            types.append(_csv_sniff_type(sample))

        final_exists = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone() is not None

        if mode == "replace" or not final_exists:
            cur.execute(f'DROP TABLE IF EXISTS "{table}"')
            cols_sql = ", ".join(f'"{c}" {t}' for c, t in zip(columns, types))
            cur.execute(f'CREATE TABLE "{table}" ({cols_sql})')
        else:
            existing = {r[1] for r in cur.execute(f'PRAGMA table_info("{table}")')}
            for c, t in zip(columns, types):
                if c not in existing:
                    cur.execute(f'ALTER TABLE "{table}" ADD COLUMN "{c}" {t}')

        converted = [tuple(_csv_convert(v, t) for v, t in zip(row, types))
                     for row in all_rows]
        if converted:
            placeholders = ", ".join("?" for _ in columns)
            col_sql = ", ".join(f'"{c}"' for c in columns)
            sql = f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders})'
            for chunk in dataops._chunks(converted, 500):
                cur.executemany(sql, chunk)
        cur.execute(f'DROP TABLE "{tmp_table}"')
        conn.commit()
