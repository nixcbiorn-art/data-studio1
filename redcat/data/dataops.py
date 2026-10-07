"""
ДВИЖОК РАБОТЫ С ДАННЫМИ
=======================
Всё чтение собранных данных идёт через этот модуль. Он ничего не знает про
недвижимость и работает с любой таблицей, которую создал сборщик.

Три принципа
------------
1. **Только чтение.** Соединение с redcat_data.db открывается в режиме
   `mode=ro` — SQLite физически не даст ничего записать, даже если в коде
   окажется опечатка. Пользовательские правки уходят в отдельную базу
   (studio_store.py) и накладываются поверх при выдаче.

2. **Имена колонок не склеиваются в SQL вслепую.** Любое имя таблицы и
   колонки сверяется с настоящей схемой через PRAGMA, значения уходят
   параметрами. Это закрывает SQL-инъекции при том, что фильтры приходят
   из браузера.

3. **Не тянуть всё в память.** Страница данных — это LIMIT/OFFSET, агрегаты
   считает сам SQLite. Приложение одинаково отзывчиво на 500 и на 500 000
   строк.
"""
from __future__ import annotations

# Фасад. Код разнесён по модулям; имена реэкспортируются, чтобы существующие
# импорты (`from redcat.data import dataops` и `dataops.X`) работали как раньше.
from redcat.data.do_core import (  # noqa: F401
    OPERATORS,
    OPERATOR_LABELS,
    AGGREGATIONS,
    AGGREGATION_LABELS,
    MAX_PAGE_SIZE,
    SQL_ROW_LIMIT,
    INTERNAL_TABLES,
    DataError,
    _Percentile,
    _Median,
    _P25,
    _P75,
    _regexp,
    connect_ro,
    list_tables,
    _columns,
    columns,
    _safe_ident,
    _check_columns,
    _is_numeric,
    _col_sql,
    _flat,
    _chunks,
)
from redcat.data.do_query import (  # noqa: F401
    build_where,
    query,
    iter_all,
)
from redcat.data.do_stats import (  # noqa: F401
    facets,
    column_stats,
    pivot,
    histogram,
    duplicates,
    outliers,
    top_bottom,
    crosstab,
)
from redcat.data.do_sql import (  # noqa: F401
    _FORBIDDEN_STMT,
    _FORBIDDEN_KEYWORD,
    _is_safe_sql,
    run_sql,
)
from redcat.data.do_formula import (  # noqa: F401
    _SAFE_EXPR,
    _DOUBLE_STAR,
    validate_formula,
    eval_formula,
)
from redcat.data.do_analytics import (  # noqa: F401
    group_summary,
    correlation,
    _describe_correlation,
    GRANULARITY,
    timeseries,
    pareto,
)
from redcat.data.do_export import (  # noqa: F401
    to_csv,
    to_json,
    iter_search_documents,
)
