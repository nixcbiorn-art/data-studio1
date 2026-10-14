"""
Реестр источников данных
========================
Ключевая идея: новый API добавляется ДЕКЛАРАТИВНО — описанием, а не кодом.
Всё остальное (сбор, нормализация, метрики, аномалии, выгрузка, графики)
работает с любым источником одинаково, потому что опирается только на это
описание.

Два способа добавить источник:

1. Положить JSON-файл в папку `sources/` (Redcat) или `sources_external/`
   (внешние). Ничего программировать не нужно — см. sources/README.
2. Вызвать `register(SourceSpec(...))` из Python, если нужна логика,
   которую в JSON не выразить.

Плейсхолдеры в URL ({region_id}, {country_id} и любые свои из .env с
префиксом REDCAT_) подставляются автоматически.
"""
from __future__ import annotations

# Фасад. Весь код разнесён по модулям reg_*; имена реэкспортируются, чтобы
# `from redcat.sources import registry as src` и `src.X` работали как раньше.
# ПОРЯДОК ИМПОРТА ВАЖЕН: модули регистрируют препроцессоры, парсеры,
# фетчеры и пагинаторы при импорте — порядок словарей сохраняется.
from redcat.sources.reg_core import (  # noqa: F401
    SourceSpec,
    _REGISTRY,
    register,
    all_sources,
    get,
    load_from_dir,
    _spec_from_dict,
    env_params,
)
from redcat.sources.reg_preprocessors import (  # noqa: F401
    PREPROCESSORS,
    register_preprocessor,
    _flatten_hc_apartments,
    _fsk_unique_projects,
    _a101_unique_projects,
    _osnova_flats,
    _mr_flats,
    _extract_estates,
    _fast_apartments,
)
from redcat.sources.reg_parsers import (  # noqa: F401
    PARSERS,
    register_parser,
    _xml_elem_to_dict,
    _parse_json_path,
    _parse_xml_tag,
    _parse_next_data,
    _parse_embedded_json,
    _parse_json_ld,
    parse_payload,
)
from redcat.sources.reg_fetch import (  # noqa: F401
    FETCHERS,
    ASYNC_FETCHERS,
    register_fetcher,
    register_async_fetcher,
    _pick_fetch_strategy,
    fetch_raw,
    async_fetch_raw,
    _import_browser_fetch,
    _fetch_browser_page,
    _fetch_browser_xhr,
)
from redcat.sources.reg_paging import (  # noqa: F401
    PAGINATORS,
    register_paginator,
    set_query_param,
    _paginated_url,
    _paginate_next_link,
    _paginate_page_number,
    _paginate_offset,
    _paginate_cursor,
    _paginate_stop,
    _paginate_auto,
    _pick_pagination_strategy,
    compute_next_url,
)
from redcat.sources.reg_normalize import (  # noqa: F401
    dig,
    _stringify_list,
    flatten_record,
    _SAFE_EXPR,
    _eval_derived,
    normalize,
)
from redcat.sources.reg_suggest import (  # noqa: F401
    PAGINATION_STYLES,
    PAGINATION_STYLE_LABELS,
    _find_first_list_path,
    _find_key_path,
    _XML_SKIP_TAGS,
    _detect_xml_record_tag,
    _detect_parse_strategy,
    suggest_spec,
)
from redcat.sources.reg_files import (  # noqa: F401
    list_source_files,
    save_source_file,
    delete_source_file,
)
