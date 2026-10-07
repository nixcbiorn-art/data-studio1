"""tg_ui — клавиатуры, текст справки и inline-кнопки по карточкам."""
from __future__ import annotations


# Кэш последних карточек для inline-кнопок. Индекс в списке идёт
# в callback_data. После перезапуска бота старые кнопки перестанут
# работать — это нормально, у пользователя всегда есть /find и /top.
_CARDS_CACHE: list = []

# ──────────────────────────────────────────────────────────────
#  Reply-клавиатура снизу
# ──────────────────────────────────────────────────────────────
# Кнопки приходят как обычный текст — то есть как /status, /top и т.д.
# Значит, отдельный маппинг не нужен, handle_command их разберёт сам.
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 /status"}, {"text": "🔝 /top 10"}],
        [{"text": "⚙️ /settings"}, {"text": "📡 /run_status"}],
        [{"text": "📋 /fill"},  {"text": "📈 /html"}],
        [{"text": "🔄 /report"}, {"text": "📁 /files"}],
        [{"text": "🔀 /diff"},   {"text": "❓ /help"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

# Reply-клавиатура — разная для admin и viewer.
MAIN_KEYBOARD_VIEWER = {
    "keyboard": [
        [{"text": "📊 /status"}, {"text": "🔝 /top 10"}],
        [{"text": "🔍 /find"},  {"text": "📋 /fill"}],
        [{"text": "📁 /files"}, {"text": "🔀 /diff"}],
        [{"text": "❓ /help"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

def main_keyboard(role: str = "viewer") -> dict:
    """Возвращает reply-клавиатуру для роли."""
    if role == "admin":
        return MAIN_KEYBOARD
    return MAIN_KEYBOARD_VIEWER

HELP_TEXT = (
    "<b>Что я умею</b>\n"
    "Внизу — кнопки для основных команд. Полный список:\n"
    "\n"
    "<b>Сбор и отчёты</b>\n"
    "/settings — настроить и запустить сбор\n"
    "/run — быстрый запуск с текущими настройками\n"
    "/run_status — что с текущим сбором\n"
    "/status — сводка по последнему отчёту\n"
    "/report — построить отчёт из уже собранного\n"
    "/top N — топ-N проблемных ЖК\n"
    "/diff — что изменилось с предыдущего отчёта\n"
    "\n"
    "<b>Поиск и разбор</b>\n"
    "/find текст — найти ЖК по названию или застройщику\n"
    "/jc название — карточка одного ЖК\n"
    "/filter dev:ЛСР — фильтр (dev: / class: / min: / max:)\n"
    "\n"
    "<b>Служебное</b>\n"
    "/files — свежие файлы в reports/\n/html — HTML-отчёты\n"
    "/log N — последние N строк tg_bot.log\n"
    "/ping — жив ли я\n"
    "/hidekbd — скрыть клавиатуру снизу\n"
    "/showkbd — вернуть клавиатуру\n"
    "/help — эта справка\n"
)

# ──────────────────────────────────────────────────────────────
#  Inline-клавиатура из карточек
# ──────────────────────────────────────────────────────────────
def _build_inline_from_cards(cards: list, max_buttons: int = 8) -> dict | None:
    """Кладёт карточки в _CARDS_CACHE и возвращает inline_keyboard."""
    global _CARDS_CACHE
    if not cards:
        return None
    _CARDS_CACHE = list(cards)
    rows = []
    for i, c in enumerate(cards[:max_buttons]):
        title = c["jc"]
        if len(title) > 40:
            title = title[:39] + "…"
        icon = {"crit": "🔴", "medium": "⚠️", "small": "·"}.get(c["cls"], "")
        text = f"{icon} {i + 1}. {title}"
        rows.append([{"text": text, "callback_data": f"jc:{i}"}])
    return {"inline_keyboard": rows}