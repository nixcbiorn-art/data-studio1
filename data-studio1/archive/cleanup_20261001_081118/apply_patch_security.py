"""
ПАТЧ — защита локального сервера + чистка дублей в app.js
=========================================================
1. webapp.py
   • проверка Host (защита от DNS rebinding) и Origin (защита от запросов
     с чужих сайтов) для всех GET/POST;
   • POST принимается только с Content-Type: application/json
     (иначе чужая страница может слать «простые» запросы без preflight);
   • source_delete / source_save_raw / GET source: имя файла режется до
     basename, чтобы «../.env» не выходило за папку источников.
2. web/app.js
   • удаляется первая (мёртвая, с неверными заголовками колонок) копия
     runCrossCheck / _renderCrossCheck / _renderCrossCheckOkBlock и
     повторное определение exportCrossCheck;
   • в оставшуюся _renderCrossCheck возвращаются подписи источников
     (внешний / внутренний).

Идемпотентно. Перед правкой делается бэкап *.bak_sec_<время>.
Запуск из папки проекта:  python apply_patch_security.py
"""
from __future__ import annotations

import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

HERE = Path(__file__).resolve().parent
WEBAPP = HERE / "webapp.py"
APPJS = HERE / "web" / "app.js"
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")


def backup(p: Path) -> Path:
    b = p.with_name(p.name + f".bak_sec_{STAMP}")
    shutil.copy2(p, b)
    return b


def replace_once(text: str, old: str, new: str, label: str, report: list) -> str:
    n = text.count(old)
    if n != 1:
        report.append(f"❌ {label}: найдено совпадений {n}, ожидалось 1 — пропущено")
        return text
    report.append(f"✅ {label}")
    return text.replace(old, new, 1)


# ──────────────────────────────────────────────────────────────
#  webapp.py
# ──────────────────────────────────────────────────────────────
GUARD_METHOD = '''    # ---------- защита локального сервера ----------
    def _guard(self, is_post: bool) -> bool:
        """Host + Origin + Content-Type. Возвращает False, если запрос отклонён."""
        allowed = getattr(self.server, "allowed_hosts", None)
        if allowed:
            host = (self.headers.get("Host") or "").lower()
            if host not in allowed:
                self._error("Недопустимый Host.", 403)
                return False
            origin = (self.headers.get("Origin") or "").lower()
            if origin and origin not in {f"http://{h}" for h in allowed}:
                self._error("Недопустимый Origin.", 403)
                return False
        if is_post:
            ctype = (self.headers.get("Content-Type") or "")
            ctype = ctype.split(";")[0].strip().lower()
            if ctype != "application/json":
                self._error("Ожидается Content-Type: application/json.", 415)
                return False
        return True

'''


def patch_webapp() -> None:
    print("\n[webapp.py]")
    if not WEBAPP.exists():
        print("  ❌ файл не найден")
        return
    text = WEBAPP.read_text(encoding="utf-8")
    if "def _guard(self, is_post" in text:
        print("  ℹ️  уже применено")
        return

    report: list = []
    new = text

    new = replace_once(
        new,
        "    # ---------- GET ----------\n    def do_GET(self):\n"
        "        parsed = urllib.parse.urlparse(self.path)\n",
        GUARD_METHOD
        + "    # ---------- GET ----------\n    def do_GET(self):\n"
          "        if not self._guard(False):\n            return\n"
          "        parsed = urllib.parse.urlparse(self.path)\n",
        "guard + do_GET", report)

    new = replace_once(
        new,
        "    def do_POST(self):\n        parsed = urllib.parse.urlparse(self.path)\n",
        "    def do_POST(self):\n        if not self._guard(True):\n            return\n"
        "        parsed = urllib.parse.urlparse(self.path)\n",
        "guard в do_POST", report)

    new = replace_once(
        new,
        '    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)\n',
        '    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)\n'
        '    server.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}\n',
        "allowed_hosts в serve()", report)

    new = replace_once(
        new,
        '        if route == "source_save_raw":\n            name = b["file"]\n',
        '        if route == "source_save_raw":\n'
        '            name = Path(str(b["file"])).name\n',
        "basename в source_save_raw", report)

    new = replace_once(
        new,
        '        if route == "source_delete":\n            name = b["file"]\n'
        '            for folder in (SOURCES_DIR, SOURCES_EXT_DIR):\n'
        '                path = folder / name\n'
        '                if path.exists():\n'
        '                    src.delete_source_file(path)\n',
        '        if route == "source_delete":\n'
        '            name = Path(str(b["file"])).name\n'
        '            if not name.endswith(".json"):\n'
        '                return self._error("Можно удалять только *.json")\n'
        '            for folder in (SOURCES_DIR, SOURCES_EXT_DIR):\n'
        '                path = folder / name\n'
        '                if path.is_file():\n'
        '                    src.delete_source_file(path)\n',
        "basename + только *.json в source_delete", report)

    new = replace_once(
        new,
        '            name = one("file", "")\n            for folder in (SOURCES_DIR, SOURCES_EXT_DIR):\n',
        '            name = Path(one("file", "")).name\n'
        '            for folder in (SOURCES_DIR, SOURCES_EXT_DIR):\n',
        "basename в GET source", report)

    for line in report:
        print("  " + line)
    if new == text:
        return

    b = backup(WEBAPP)
    WEBAPP.write_text(new, encoding="utf-8", newline="")
    try:
        py_compile.compile(str(WEBAPP), doraise=True, cfile=str(WEBAPP) + ".pyc")
    except py_compile.PyCompileError as e:
        print(f"  ❌ не компилируется, откатываю: {e}")
        shutil.copy2(b, WEBAPP)
        return
    finally:
        Path(str(WEBAPP) + ".pyc").unlink(missing_ok=True)
    print(f"  💾 бэкап: {b.name}")


# ──────────────────────────────────────────────────────────────
#  app.js
# ──────────────────────────────────────────────────────────────
MARKER = "  /* ── СВЕРКА С REDCAT ─"
EXPORT_SIG = "  exportCrossCheck(format) {"

OLD_META = (
    "    const meta = el('div', { class: 'tiny muted', style: 'margin-bottom:11px' },\n"
    "      `Сопоставление: «${r.on_left}» ↔ «${r.on_right}», ` +\n"
)
NEW_META = (
    "    const lLabel = r.left_label || r.source || 'внешний';\n"
    "    const rLabel = r.right_label || r.with_table || 'внутренний';\n"
    "    const lTag = r.left_external ? 'внешний' : 'внутренний';\n"
    "    const rTag = r.right_external ? 'внешний' : 'внутренний';\n"
    "    const meta = el('div', { class: 'tiny muted', style: 'margin-bottom:11px' },\n"
    "      `Сопоставление: «${r.on_left}» (${lLabel}, ${lTag}) ↔ "
    "«${r.on_right}» (${rLabel}, ${rTag}), ` +\n"
)


def patch_appjs() -> None:
    print("\n[web/app.js]")
    if not APPJS.exists():
        print("  ❌ файл не найден")
        return
    text = APPJS.read_text(encoding="utf-8")
    report: list = []
    new = text

    if new.count("async runCrossCheck()") < 2 and new.count(EXPORT_SIG) < 2:
        print("  ℹ️  дублей нет")
    else:
        # 1. первая копия: от первого маркера до второго
        if new.count(MARKER) >= 2:
            i1 = new.index(MARKER)
            i2 = new.index(MARKER, i1 + 1)
            new = new[:i1] + new[i2:]
            report.append("✅ удалена первая копия runCrossCheck/_render*")
        else:
            report.append("❌ не найдены два маркера «СВЕРКА С REDCAT» — "
                          "дубли render/run уберите вручную")

        # 2. повторный exportCrossCheck (оставляем первый)
        if new.count(EXPORT_SIG) >= 2:
            first = new.index(EXPORT_SIG)
            second = new.index(EXPORT_SIG, first + 1)
            end = new.index("\n  },\n", second) + len("\n  },\n")
            new = new[:second] + new[end:]
            report.append("✅ удалён повторный exportCrossCheck")

    # 3. подписи источников
    if "const lLabel = r.left_label" not in new:
        new = replace_once(new, OLD_META, NEW_META,
                           "подписи источников в _renderCrossCheck", report)

    for line in report:
        print("  " + line)
    if new != text:
        b = backup(APPJS)
        APPJS.write_text(new, encoding="utf-8", newline="")
        print(f"  💾 бэкап: {b.name}")
        print(f"  runCrossCheck теперь определён раз: "
              f"{new.count('async runCrossCheck()')}")


if __name__ == "__main__":
    print("=" * 60)
    print("  Патч безопасности сервера + чистка app.js")
    print("=" * 60)
    patch_webapp()
    patch_appjs()
    print("\nДальше: перезапустите studio.py (Ctrl+C, затем python studio.py)")
    print("и обновите страницу через Ctrl+F5.")
