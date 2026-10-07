"""
СЕРВЕР ПРИЛОЖЕНИЯ
=================
Локальное веб-приложение поверх собранных данных. Работает на стандартной
библиотеке Python — ставить ничего не нужно, интернет не требуется.

Про запросы и «никаких PUT в API»
---------------------------------
  • Сторонний API — приложение обращается к нему ИСКЛЮЧИТЕЛЬНО методом GET.
    Это заблокировано на уровне сетевых библиотек (api_guard.py). Для
    источников с fetch_mode="browser" то же правило работает внутри
    Playwright (см. browser_fetch.py::_route_filter).

  • Этот локальный сервер (127.0.0.1) — браузер разговаривает с ним же на
    вашем компьютере. Чтение идёт через GET, сохранение правок — через
    POST на localhost. POST никуда не уходит: он пишет строку в файл
    studio.db на вашем диске.

Две базы данных
---------------
Redcat и внешние источники хранятся раздельно:
  • reports/redcat_data.db     — источники из папки sources/
  • reports/external_data.db   — источники из папки sources_external/
Куда писать, определяется полем external в spec (см. sources.py).
Сервер сам определяет нужную БД по имени таблицы (db_for_table).

Сверка источников
-----------------
Маршрут /api/cross_check?source=<внешний_ключ> сопоставляет таблицу
внешнего источника с другой таблицей (обычно apartments из Redcat) по
правилам из секции cross_check в spec. Плюс подмешивает общий словарь
синонимов из hc_aliases.json, если он есть.

Сервер слушает только 127.0.0.1, то есть недоступен из сети.
"""
from __future__ import annotations

import socket
import threading
from http.server import ThreadingHTTPServer
from redcat.core import api_guard
from redcat.sources import hc_aliases
from redcat.data import studio_store as store
from redcat.web.web_config import DATA_DB, EXTERNAL_DB, STUDIO_DB
from redcat.web.web_handler import Handler


# ──────────────────────────────────────────────────────────────

# ── Обратная совместимость ─────────────────────────────────────────────
# Раньше всё жило в одном файле: другие модули (source_stats, jc_compare,
# export_problem_jc, scan_sources, history_dashboard) вызывают webapp.load_specs,
# webapp.db_for_table и т. п. Реэкспорт сохраняет эти обращения рабочими.
from redcat.web.web_config import (  # noqa: F401
    BASE_DIR,
    ENV_FILE,
    HISTORY_DIR,
    OUTPUT_DIR,
    SOURCES_DIR,
    SOURCES_EXT_DIR,
    STATS_DB,
    WEB_DIR,
    _pct_thr,
)
from redcat.web.web_crosscheck import (  # noqa: F401
    _cross_check_report,
)
from redcat.web.web_csv import (  # noqa: F401
    _CSV_UPLOAD_STATE,
    _csv_convert,
    _csv_dedupe_idents,
    _csv_pad_row,
    _csv_sniff_type,
)
from redcat.web.web_data import (  # noqa: F401
    _aggregate,
    _external_table_names,
    _json_safe,
    _normalize_key,
    _retype,
    anomalies_list,
    db_for_table,
    decorate,
    id_field_for,
    load_specs,
    name_field_for,
    parse_query,
    record_series,
    resolve_sql_db,
    runs_history,
    source_files,
    sql_databases,
)
from redcat.web.web_filters import (  # noqa: F401
    _apply_virtual_filters,
    _sanitize_filters,
)
from redcat.web.web_http_util import (  # noqa: F401
    MAX_BODY_BYTES,
    _REDCAT_HOSTS,
    _is_redcat_host,
)
from redcat.web.web_runners import (  # noqa: F401
    RUNNER,
    ScraperRunner,
    TOOLS,
    TOOLS_RUNNER,
    ToolsRunner,
    tools_list,
)
from redcat.web.web_telegram import (  # noqa: F401
    _clean_token,
    read_env_token,
    telegram_probe,
    telegram_status,
    token_status,
    write_env_token,
    write_telegram_env,
)


def find_free_port(preferred=8765) -> int:
    for port in range(preferred, preferred + 25):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def serve(port=8765, open_browser=True):
    store.init(STUDIO_DB)
    store.prune_api_log(STUDIO_DB)
    port = find_free_port(port)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    url = f"http://127.0.0.1:{port}/"

    print("=" * 60)
    print("  REDCAT STUDIO")
    print("=" * 60)
    print(f"  {api_guard.status_text()}")
    print(f"  Данные:  {DATA_DB if DATA_DB.exists() else 'ещё не собраны'}")
    if EXTERNAL_DB.exists():
        print(f"  Внешние: {EXTERNAL_DB}")
    n_aliases = hc_aliases.count()
    if n_aliases:
        print(f"  Синонимов ЖК: {n_aliases} (hc_aliases.json)")
    print(f"  Правки:  {STUDIO_DB.name} (локально, в API не уходят)")
    print(f"\n  Откройте в браузере:  {url}")
    print("\n  Остановить — Ctrl+C\n")

    if open_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
