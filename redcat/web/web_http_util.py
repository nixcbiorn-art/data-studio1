"""web_http_util — проверка Host и лимит тела запроса."""
from __future__ import annotations



# ──────────────────────────────────────────────────────────────
#  МАРШРУТИЗАЦИЯ
# ──────────────────────────────────────────────────────────────
# Хосты, на которые можно отправлять Redcat-токен.
_REDCAT_HOSTS = ("redcat.ai",)


def _is_redcat_host(url: str) -> bool:
    """URL ведёт на *.redcat.ai (или сам redcat.ai)?"""
    try:
        import urllib.parse
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        return any(host == h or host.endswith("." + h)
                   for h in _REDCAT_HOSTS)
    except Exception:
        return False


# Лимит размера тела запроса: пачка CSV из 1000 строк
# укладывается с большим запасом, а 10-гигабайтный
# запрос не займёт сервер навсегда.
MAX_BODY_BYTES = 50 * 1024 * 1024   # 50 МБ
