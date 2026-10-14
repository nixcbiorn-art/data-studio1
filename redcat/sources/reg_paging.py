"""reg_paging — пагинаторы (регистрируются в PAGINATORS) и compute_next_url."""
from __future__ import annotations

from redcat.sources.reg_normalize import dig


# ──────────────────────────────────────────────────────────────
#  ПАГИНАЦИИ
# ──────────────────────────────────────────────────────────────
# Пагинатор принимает:
#   data          — raw payload ответа (то, что вернул парсер в "raw")
#   spec          — SourceSpec
#   current_url   — URL только что полученной страницы
#   page_number   — её номер
#   got_rows      — сколько записей было на этой странице
#   collected     — сколько собрано всего
#   total         — сколько заявлено API (или None)
# Возвращает URL следующей страницы или None, если обход завершён.
#
# Регистрация:
#     @register_paginator("my_mode")
#     def _paginate_my_mode(...): ...
PAGINATORS: dict = {}


def register_paginator(name: str):
    def deco(fn):
        PAGINATORS[name] = fn
        return fn
    return deco


def set_query_param(url: str, name: str, value) -> str:
    """Заменяет или добавляет query-параметр в URL (с сохранением остальных)."""
    import urllib.parse as _up
    parts = _up.urlsplit(url)
    pairs = _up.parse_qsl(parts.query, keep_blank_values=True)
    pairs = [(k, v) for k, v in pairs if k != name]
    pairs.append((name, str(value)))
    return _up.urlunsplit(parts._replace(query=_up.urlencode(pairs)))


def _paginated_url(current_url, spec, page_number, collected):
    """Стандартная «следующая страница»: page_size + (номер или offset)."""
    is_offset = "offset" in (spec.page_number_param or "").lower()
    value = collected if is_offset else page_number + 1
    url = set_query_param(current_url, spec.page_size_param, spec.page_size)
    url = set_query_param(url, spec.page_number_param, value)
    return url


@register_paginator("next_link")
def _paginate_next_link(data, spec, current_url, page_number, got_rows,
                        collected, total):
    """Идём ровно по ссылке из ответа; если её нет — обход завершён."""
    import urllib.parse as _up
    nxt = dig(data, spec.next_link_path)
    if not isinstance(nxt, str) or not nxt:
        return None
    absolute = _up.urljoin(current_url, nxt)
    return set_query_param(absolute, spec.page_size_param, spec.page_size)


@register_paginator("page_number")
def _paginate_page_number(data, spec, current_url, page_number, got_rows,
                          collected, total):
    if total is not None and collected >= total:
        return None
    if total is None and got_rows < spec.page_size:
        return None
    return _paginated_url(current_url, spec, page_number, collected)


@register_paginator("offset")
def _paginate_offset(data, spec, current_url, page_number, got_rows,
                     collected, total):
    if total is not None and collected >= total:
        return None
    if got_rows == 0:
        return None
    return _paginated_url(current_url, spec, page_number, collected)


@register_paginator("cursor")
def _paginate_cursor(data, spec, current_url, page_number, got_rows,
                     collected, total):
    """Курсорная пагинация: следующий курсор лежит по spec.cursor_path."""
    if not spec.cursor_path:
        return None
    cursor = dig(data, spec.cursor_path)
    if cursor is None or cursor == "":
        return None
    return set_query_param(current_url, "cursor", cursor)


@register_paginator("stop")
def _paginate_stop(data, spec, current_url, page_number, got_rows,
                   collected, total):
    return None


@register_paginator("auto")
def _paginate_auto(data, spec, current_url, page_number, got_rows,
                   collected, total):
    """Приоритет next_link (если он есть в ответе), иначе номер/offset."""
    import urllib.parse as _up
    nxt = dig(data, spec.next_link_path) if spec.next_link_path else None
    if isinstance(nxt, str) and nxt:
        absolute = _up.urljoin(current_url, nxt)
        return set_query_param(absolute, spec.page_size_param, spec.page_size)
    if total is not None and collected >= total:
        return None
    if total is None and got_rows < spec.page_size:
        return None
    return _paginated_url(current_url, spec, page_number, collected)


def _pick_pagination_strategy(spec) -> str:
    if getattr(spec, "pagination_strategy", ""):
        return spec.pagination_strategy
    if "offset" in (spec.page_number_param or "").lower():
        return "offset"
    return "auto"


def compute_next_url(data, spec, current_url, page_number, got_rows,
                     collected, total):
    """Диспетчер: возвращает URL следующей страницы или None."""
    name = _pick_pagination_strategy(spec)
    pager = PAGINATORS.get(name)
    if pager is None:
        raise ValueError(
            f"Источник «{spec.key}»: неизвестная стратегия пагинации "
            f"«{name}». Доступные: {', '.join(sorted(PAGINATORS))}.")
    return pager(data, spec, current_url, page_number, got_rows,
                 collected, total)
