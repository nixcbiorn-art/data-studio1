"""tg_commands — обработчик текстовых команд бота."""
from __future__ import annotations

import re
import time
from datetime import datetime
from redcat.bot.tg_api import _api_call, _html_escape, _reply, edit_message_text, send_document
from redcat.bot.tg_common import BOT_LOG, EXPORT_LOG, LOG, REPORTS, bot_users
from redcat.bot import tg_common
from redcat.bot.tg_fill import _fill_keyboard, _fill_summary_all, _fill_table_detailed, _load_specs_safe
from redcat.bot.tg_html import HTML_REPORTS, _html_generate, _html_list_files, _html_menu_keyboard, _html_menu_text
from redcat.bot import tg_reports
from redcat.bot.tg_reports import _diff_reports, _filter_cards, _find_in_report, _find_latest, _format_card_full, _parse_md_full, build_report_caption, build_status_text, build_top_text, run_export
from redcat.bot.tg_scraper_ctl import _scraper_start, _scraper_status_text
from redcat.bot.tg_settings import _list_known_sources, _load_settings, _settings_keyboard, _settings_text, _settings_to_args
from redcat.bot.tg_ui import HELP_TEXT, _build_inline_from_cards, main_keyboard

def handle_command(token: str, chat_id: str, text: str,
                   role: str = "viewer") -> None:
    # Проверка роли и прав (bot_users).
    if bot_users is not None:
        _role_now = bot_users.get_role(chat_id)
        if _role_now is None:
            # Может быть, это переход по приглашению:
            # /start <код> (deep link из ссылки t.me/...)
            _invited = False
            _s = (text or "").strip()
            if _s.startswith("/start"):
                _parts = _s.split(maxsplit=1)
                if len(_parts) > 1:
                    _code = _parts[1].strip()
                    _ok, _new_role, _msg = bot_users.use_invite(
                        _code, chat_id)
                    if _ok:
                        _role_now = _new_role
                        _invited = True
                        LOG.info("по приглашению %s: %s → %s",
                                 _code, chat_id, _new_role)
                        _reply(token, chat_id,
                               f"✅ Приглашение принято. Ваша роль: "
                               f"<b>{_new_role}</b>\n\n"
                               f"Напишите /help — покажу, что умею.",
                               reply_markup=main_keyboard(_new_role))
                    else:
                        LOG.warning("плохой код %r от %s: %s",
                                    _code, chat_id, _msg)
            if not _invited:
                bot_users.add_pending(chat_id, "", text)
                LOG.warning("сообщение от %s — не в списке", chat_id)
                return
        role = _role_now
    LOG.info("команда от %s: %s", chat_id, (text or "").strip()[:120])
    raw = (text or "").strip()
    # Reply-кнопки снизу подписаны с эмодзи: «📁 /files». Telegram
    # присылает текст КАК ЕСТЬ — то есть наша проверка startswith("/")
    # на них падала, и бот отвечал «Не понял». Поэтому ищем команду
    # в любом месте строки: первое /<буквы>, до конца строки — команда
    # и её аргументы. Всё, что было до этого (эмодзи, пробелы, «нажми»),
    # отбрасываем.
    m = re.search(r"/([A-Za-zА-Яа-яЁё_]+)(?:@\w+)?(?:\s+(.*))?$", raw)
    if not m:
        _reply(token, chat_id, "Не понял. /help — что я умею.")
        return
    cmd = "/" + m.group(1).lower()
    args = (m.group(2) or "").split()
    LOG.debug("разобрано: cmd=%r args=%r", cmd, args)

    if cmd in ("/start", "/help"):
        _reply(token, chat_id, HELP_TEXT, reply_markup=main_keyboard(role))

    elif cmd == "/showkbd":
        _reply(token, chat_id, "Клавиатура снизу вернулась.",
               reply_markup=main_keyboard(role))

    elif cmd == "/hidekbd":
        _reply(token, chat_id, "Клавиатура снизу скрыта. "
                               "Вернуть — /showkbd.",
               reply_markup={"remove_keyboard": True})

    elif cmd == "/ping":
        _reply(token, chat_id,
               f"🏓 Жив. Время сервера: {datetime.now():%d.%m.%Y %H:%M:%S}")

    elif cmd == "/status":
        _reply(token, chat_id, build_status_text())

    elif cmd == "/top":
        n = 10
        if args:
            try:
                n = max(1, min(int(args[0]), 25))
            except ValueError:
                pass
        md = _find_latest("problem_jc_*.md")
        if not md:
            _reply(token, chat_id,
                   "Отчёта ещё нет. Сначала <code>/report</code>.")
            return
        cards = _parse_md_full(md)[:n]
        if not cards:
            _reply(token, chat_id, "В последнем отчёте проблем нет. ✅")
            return
        kb = _build_inline_from_cards(cards, max_buttons=n)
        _reply(token, chat_id, build_top_text(n), reply_markup=kb)

    elif cmd == "/report":
        _reply(token, chat_id, "⏳ Запускаю сбор… Это может занять минуту.")
        LOG.info("/report начат (chat_id=%s)", chat_id)
        _t0 = time.time()
        ok, md, csv, disc = run_export()
        LOG.info("/report завершён: ok=%s, за %.1f сек",
                 ok, time.time() - _t0)
        if not ok:
            err = tg_reports._LAST_EXPORT_ERROR or "(нет вывода)"
            if len(err) > 3500:
                err = err[-3500:]
            _reply(token, chat_id,
                   "❌ <b>Не удалось построить отчёт.</b>\n\n"
                   f"<pre>{_html_escape(err)}</pre>\n\n"
                   f"Полный лог: <code>{_html_escape(EXPORT_LOG.name)}</code>")
            return
        if md:
            send_document(token, chat_id, md, caption=build_report_caption(md))
        if csv:
            send_document(token, chat_id, csv, caption="Список проблем (CSV)")
        if disc:
            send_document(token, chat_id, disc, caption="Скидочные (CSV)")
        _reply(token, chat_id, build_status_text(), reply_markup=main_keyboard(role))

    elif cmd == "/diff":
        _reply(token, chat_id, _diff_reports())

    elif cmd == "/find":
        q = " ".join(args).strip()
        if not q:
            _reply(token, chat_id, "Укажите текст: <code>/find ЗИЛАРТ</code>")
            return
        md = _find_latest("problem_jc_*.md")
        if not md:
            _reply(token, chat_id,
                   "Отчёта ещё нет. Сначала <code>/report</code>.")
            return
        cards = _find_in_report(md, q, limit=15)
        if not cards:
            _reply(token, chat_id,
                   f"Ничего не найдено по «{_html_escape(q)}».")
            return
        lines = [f"<b>Найдено: {len(cards)}</b>",
                 "<i>Нажмите на ЖК под сообщением — покажу карточку.</i>",
                 ""]
        for i, c in enumerate(cards, 1):
            icon = {"crit": "🔴", "medium": "⚠️",
                    "small": "·"}.get(c["cls"], "·")
            lines.append(f"{i}. {icon} <b>{_html_escape(c['jc'])}</b> "
                         f"({_html_escape(c['dev'])}) — Δ "
                         f"{_html_escape(c['dpct'])}")
        kb = _build_inline_from_cards(cards)
        _reply(token, chat_id, "\n".join(lines), reply_markup=kb)

    elif cmd == "/jc":
        q = " ".join(args).strip()
        if not q:
            _reply(token, chat_id,
                   "Укажите название: <code>/jc ЗИЛАРТ</code>")
            return
        md = _find_latest("problem_jc_*.md")
        if not md:
            _reply(token, chat_id,
                   "Отчёта ещё нет. Сначала <code>/report</code>.")
            return
        cards = _find_in_report(md, q, limit=3)
        if not cards:
            _reply(token, chat_id, f"Не нашёл ЖК по «{_html_escape(q)}».")
            return
        _reply(token, chat_id, _format_card_full(cards[0]))
        if len(cards) > 1:
            # Похожие — inline-кнопки, чтобы кликнуть в один тап.
            kb = _build_inline_from_cards(cards[1:], max_buttons=3)
            _reply(token, chat_id,
                   "<i>Похожие:</i>",
                   reply_markup=kb)

    elif cmd == "/filter":
        expr = " ".join(args).strip()
        md = _find_latest("problem_jc_*.md")
        if not md:
            _reply(token, chat_id,
                   "Отчёта ещё нет. Сначала <code>/report</code>.")
            return
        cards = _parse_md_full(md)
        if not expr:
            _reply(token, chat_id,
                   "Фильтр пустой. Примеры:\n"
                   "<code>/filter dev:ЛСР</code>\n"
                   "<code>/filter class:crit</code>\n"
                   "<code>/filter min:10</code>\n"
                   "Можно через запятую: "
                   "<code>/filter dev:А101,min:5</code>")
            return
        picked = _filter_cards(cards, expr)
        if not picked:
            _reply(token, chat_id,
                   f"Ничего не подходит под «{_html_escape(expr)}».")
            return
        lines = [f"<b>Подходит: {len(picked)} из {len(cards)}</b>",
                 "<i>Нажмите на ЖК — покажу карточку.</i>", ""]
        for i, c in enumerate(picked[:25], 1):
            icon = {"crit": "🔴", "medium": "⚠️",
                    "small": "·"}.get(c["cls"], "·")
            lines.append(f"{i}. {icon} {_html_escape(c['jc'])} "
                         f"({_html_escape(c['dev'])}) — Δ "
                         f"{_html_escape(c['dpct'])}")
        if len(picked) > 25:
            lines.append(f"… и ещё {len(picked) - 25}")
        kb = _build_inline_from_cards(picked)
        _reply(token, chat_id, "\n".join(lines), reply_markup=kb)

    elif cmd == "/settings":
        s = _load_settings()
        srcs = _list_known_sources()
        _reply(token, chat_id, _settings_text(s, srcs),
               reply_markup=_settings_keyboard(s, srcs))

    elif cmd == "/run":
        if not args:
            args = _settings_to_args(_load_settings())
        ok, msg = _scraper_start(chat_id, token, args)
        _reply(token, chat_id, msg)

    elif cmd == "/run_status":
        _reply(token, chat_id, _scraper_status_text())

    elif cmd == "/fill":
        # Считаем медленно, поэтому сразу отправляем «считаю…» через
        # _api_call, чтобы получить message_id, потом редактируем это
        # же сообщение на результат.
        specs = _load_specs_safe()
        if args:
            table = args[0]
            sent = _api_call(
                "sendMessage", token, chat_id=chat_id,
                text=f"⏳ Считаю «{_html_escape(table)}»…",
                parse_mode="HTML")
            try:
                msg_id = sent["result"]["message_id"]
            except (TypeError, KeyError):
                msg_id = None
            body = _fill_table_detailed(table, specs)
            kb = {"inline_keyboard": [[
                {"text": "◀ ко всем таблицам",
                 "callback_data": "fill:refresh"}
            ]]}
            if msg_id:
                edit_message_text(token, chat_id, msg_id, body,
                                  reply_markup=kb)
            else:
                _reply(token, chat_id, body, reply_markup=kb)
        else:
            sent = _api_call(
                "sendMessage", token, chat_id=chat_id,
                text="⏳ Считаю заполненность по всем таблицам…",
                parse_mode="HTML")
            try:
                msg_id = sent["result"]["message_id"]
            except (TypeError, KeyError):
                msg_id = None
            body = _fill_summary_all(specs)
            kb = _fill_keyboard()
            if msg_id:
                edit_message_text(token, chat_id, msg_id, body,
                                  reply_markup=kb)
            else:
                _reply(token, chat_id, body, reply_markup=kb)

    elif cmd == "/html":
        # /html            — меню
        # /html history    — генерация и отправка history.html
        # /html completeness
        # /html dashboard  — только существующий файл
        # /html files      — список всех HTML
        if not args:
            _reply(token, chat_id, _html_menu_text(),
                   reply_markup=_html_menu_keyboard())
        else:
            sub = args[0].lower()
            if sub in ("files", "list"):
                _reply(token, chat_id, _html_list_files())
            elif sub in HTML_REPORTS:
                cfg = HTML_REPORTS[sub]
                if cfg["script"] is None:
                    if cfg["output"].exists():
                        send_document(token, chat_id, cfg["output"],
                                      caption=cfg["caption"])
                    else:
                        _reply(token, chat_id,
                               f"❌ <code>{cfg['output'].name}</code> "
                               f"ещё не создан. Запусти /run.")
                else:
                    sent = _api_call(
                        "sendMessage", token, chat_id=chat_id,
                        text=f"⏳ Генерирую "
                             f"<b>{_html_escape(cfg['title'])}</b>…",
                        parse_mode="HTML")
                    try:
                        msg_id = sent["result"]["message_id"]
                    except (TypeError, KeyError):
                        msg_id = None
                    ok, msg, path = _html_generate(sub)
                    if not ok:
                        if msg_id:
                            edit_message_text(token, chat_id, msg_id, msg)
                        else:
                            _reply(token, chat_id, msg)
                    else:
                        if msg_id:
                            edit_message_text(
                                token, chat_id, msg_id,
                                f"✅ Готово: "
                                f"<code>{_html_escape(path.name)}</code>")
                        send_document(token, chat_id, path,
                                      caption=cfg["caption"])
            else:
                _reply(token, chat_id,
                       f"Неизвестный отчёт «{_html_escape(sub)}». "
                       f"Доступные: history, completeness, dashboard, files.")

    elif cmd == "/files":
        if not REPORTS.exists():
            _reply(token, chat_id, "Папка reports/ не найдена.")
            return
        items = [p for p in REPORTS.iterdir()
                 if p.is_file() and not p.name.startswith("_")]
        items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        items = items[:15]
        if not items:
            _reply(token, chat_id, "В reports/ пока пусто.")
            return
        lines = ["<b>Свежие файлы в reports/</b>", ""]
        for p in items:
            sz = p.stat().st_size
            unit = "Б"
            if sz >= 1024 * 1024:
                sz, unit = sz // (1024 * 1024), "МБ"
            elif sz >= 1024:
                sz, unit = sz // 1024, "КБ"
            when = datetime.fromtimestamp(
                p.stat().st_mtime).strftime("%d.%m %H:%M")
            lines.append(f"• <code>{_html_escape(p.name)}</code> — "
                         f"{sz} {unit}, {when}")
        _reply(token, chat_id, "\n".join(lines))

    elif cmd == "/log":
        n = 30
        if args:
            try:
                n = max(1, min(int(args[0]), 200))
            except ValueError:
                pass
        if not BOT_LOG.exists():
            _reply(token, chat_id, "Лог tg_bot.log ещё не создан.")
            return
        try:
            tail = BOT_LOG.read_text(
                encoding="utf-8", errors="replace").splitlines()[-n:]
        except OSError as e:
            _reply(token, chat_id, f"Не смог прочитать лог: {e}")
            return
        if not tail:
            _reply(token, chat_id, "Лог пуст.")
            return
        tail = [(t[:200] + "…" if len(t) > 200 else t) for t in tail]
        _reply(token, chat_id,
               f"<b>Последние {len(tail)} строк лога:</b>\n"
               f"<pre>{_html_escape(chr(10).join(tail))}</pre>")

    elif cmd == "/invite":
        _role = "viewer"
        if args:
            _role = args[0].lower()
            if _role not in ("viewer", "admin"):
                _reply(token, chat_id,
                       "Формат: <code>/invite [viewer|admin]</code>")
                return
        _code, _expires = bot_users.create_invite(_role, chat_id)
        _url = (f"https://t.me/{tg_common._BOT_USERNAME}?start={_code}"
                if tg_common._BOT_USERNAME else f"/start {_code}")
        _reply(token, chat_id,
               f"🎟 <b>Приглашение создано</b>\n\n"
               f"Роль: <b>{_role}</b>\n"
               f"Код: <code>{_code}</code>\n"
               f"Срок: до {_expires[:16].replace('T', ' ')}\n"
               f"Одноразовый.\n\n"
               f"Отправьте ссылку человеку:\n"
               f"<code>{_url}</code>\n\n"
               f"Если ссылка не открывается — пусть напишет боту:\n"
               f"<code>/start {_code}</code>")

    elif cmd == "/invites":
        _invs = bot_users.list_invites()
        if not _invs:
            _reply(token, chat_id, "Активных приглашений нет.")
            return
        lines = ["<b>Приглашения</b>", ""]
        for inv in _invs[:30]:
            if inv["used_by"]:
                status = f"✅ использован ({inv['used_by']})"
            elif inv["expired"]:
                status = "⌛ истёк"
            else:
                status = "🟢 активен"
            lines.append(
                f"• <code>{inv['code']}</code> — "
                f"<b>{inv['role']}</b> — {status}")
        lines += ["", "Отозвать: <code>/revoke &lt;код&gt;</code>"]
        _reply(token, chat_id, "\n".join(lines))

    elif cmd == "/revoke":
        if not args:
            _reply(token, chat_id, "Формат: <code>/revoke &lt;код&gt;</code>")
            return
        ok, msg = bot_users.revoke_invite(args[0])
        _reply(token, chat_id, ("✅ " if ok else "❌ ") + msg)

    elif cmd == "/users":
        _users_text = "<b>Пользователи бота</b>\n\n"
        for u in bot_users.list_users():
            _users_text += (f"• <code>{_html_escape(u['chat_id'])}</code> — "
                            f"<b>{_html_escape(u['role'])}</b>"
                            + (f" ({_html_escape(u['name'])})" if u['name'] else "")
                            + f"\n  добавлен: {_html_escape(u['added_at'])}\n")
        _reply(token, chat_id, _users_text)

    elif cmd == "/pending":
        pending = bot_users.list_pending()
        if not pending:
            _reply(token, chat_id, "Ожидающих нет.")
            return
        lines = ["<b>Писали боту, но не в списке:</b>", ""]
        for p in pending[:30]:
            lines.append(
                f"• <code>{_html_escape(p['chat_id'])}</code>"
                + (f" {_html_escape(p['name'])}" if p["name"] else "")
                + f" — попыток {p['attempts']}, последнее: "
                  f"{_html_escape(p['last_text'][:60])}")
        lines += ["", "Добавить: <code>/add_user &lt;id&gt; [viewer|admin]</code>"]
        _reply(token, chat_id, "\n".join(lines))

    elif cmd == "/add_user":
        if not args:
            _reply(token, chat_id,
                   "Формат: <code>/add_user &lt;chat_id&gt; "
                   "[viewer|admin]</code>")
            return
        _cid = args[0]
        _role = args[1].lower() if len(args) > 1 else "viewer"
        ok, msg = bot_users.add_user(_cid, _role)
        _reply(token, chat_id, ("✅ " if ok else "❌ ") + msg)

    elif cmd == "/remove_user":
        if not args:
            _reply(token, chat_id,
                   "Формат: <code>/remove_user &lt;chat_id&gt;</code>")
            return
        ok, msg = bot_users.remove_user(args[0])
        _reply(token, chat_id, ("✅ " if ok else "❌ ") + msg)

    elif cmd == "/set_role":
        if len(args) < 2:
            _reply(token, chat_id,
                   "Формат: <code>/set_role &lt;chat_id&gt; "
                   "[viewer|admin]</code>")
            return
        ok, msg = bot_users.set_role(args[0], args[1].lower())
        _reply(token, chat_id, ("✅ " if ok else "❌ ") + msg)

    else:
        _reply(token, chat_id,
               f"Неизвестная команда: {cmd}. /help — что я умею.")