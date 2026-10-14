#!/usr/bin/env python3
"""
migrate_layout.py — раскладка плоского проекта по пакетам redcat/.
==================================================================
Логика не меняется: только переносы файлов, импорты, пути и запуск
дочерних скриптов (теперь через `python -m`).

    python migrate_layout.py            # dry-run: покажет план
    python migrate_layout.py --apply    # применить (резервные *.bak не создаёт —
                                        # работайте в git-ветке!)

Итоговая структура:
    redcat/core/       api_guard, paths, runner
    redcat/data/       dataops, thresholds, storage, studio_store, run_stats,
                       field_labels, field_semantics
    redcat/sources/    registry (бывш. sources.py), name_normalizer,
                       hc_aliases, spec_validator
    redcat/collection/ redcat_scraper, collect_pool, rate_limit,
                       browser_fetch, diagnose
    redcat/quality/    anomalies, completeness, crosschecks, hc_crosscheck,
                       drift (бывш. quality.py)
    redcat/reporting/  report_html, completeness_dashboard, history_dashboard,
                       source_stats, export_problem_jc, jc_compare
    redcat/web/        webapp + static/ (бывш. web/)
    redcat/bot/        tg_bot, bot_users
    redcat/tools/      launcher, set_token, set_telegram, sql, check_runtime,
                       selftest_readonly, demo_data, make_field_labels,
                       fill_aliases, match_complex_names, scan_sources
    config/            sources/, sources_external/, field_semantics.json,
                       hc_aliases.json
    tests/             test_name_normalizer
    scripts/legacy/    одноразовые патчи (fix_osnova_pagination, clean_git_commit)
    studio.py          точка входа (остаётся в корне)
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# модуль -> (пакет, новое имя модуля)
MODULES: dict[str, tuple[str, str]] = {}
for _pkg, _names in {
    "core": ["api_guard"],
    "data": ["dataops", "thresholds", "storage", "studio_store", "run_stats",
             "field_labels", "field_semantics"],
    "sources": ["sources:registry", "name_normalizer", "hc_aliases",
                "spec_validator"],
    "collection": ["redcat_scraper", "collect_pool", "rate_limit",
                   "browser_fetch", "diagnose"],
    "quality": ["anomalies", "completeness", "crosschecks", "hc_crosscheck",
                "quality:drift"],
    "reporting": ["report_html", "completeness_dashboard", "history_dashboard",
                  "source_stats", "export_problem_jc", "jc_compare"],
    "web": ["webapp"],
    "bot": ["tg_bot", "bot_users"],
    "tools": ["launcher", "set_token", "set_telegram", "sql", "check_runtime",
              "selftest_readonly", "demo_data", "make_field_labels",
              "fill_aliases", "match_complex_names", "scan_sources"],
}.items():
    for _n in _names:
        _old, _, _new = _n.partition(":")
        MODULES[_old] = (_pkg, _new or _old)

OTHER_MOVES = {
    "test_name_normalizer.py": "tests/test_name_normalizer.py",
    "fix_osnova_pagination.py": "scripts/legacy/fix_osnova_pagination.py",
    "clean_git_commit.py": "scripts/legacy/clean_git_commit.py",
    "field_semantics.json": "config/field_semantics.json",
    "hc_aliases.json": "config/hc_aliases.json",
}
DIR_MOVES = {
    "sources": "config/sources",
    "sources_external": "config/sources_external",
}
STATIC_FILES = ["index.html", "app.js", "style.css", "map.html"]

PACKAGES = ["redcat"] + [f"redcat/{p}" for p in
            ["core", "data", "sources", "collection", "quality", "reporting",
             "web", "bot", "tools"]]

PATHS_PY = '''"""Единственное место, где определены пути проекта."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = ROOT / "config"
SOURCES_DIR = CONFIG_DIR / "sources"
SOURCES_EXT_DIR = CONFIG_DIR / "sources_external"

WEB_DIR = ROOT / "redcat" / "web" / "static"

# Данные, создаваемые при работе (не в git)
REPORTS_DIR = ROOT / "reports"
HISTORY_DIR = ROOT / "history"
ENV_FILE = ROOT / ".env"
STUDIO_DB = ROOT / "studio.db"
'''

RUNNER_PY = '''"""Запуск дочерних скриптов проекта через `python -m`.

Раньше скрипты лежали в корне и запускались по имени файла. Теперь они в
пакетах, поэтому имя файла («redcat_scraper.py») сопоставляется с модулем.
"""
from __future__ import annotations

import sys
from pathlib import Path

from redcat.core.paths import ROOT

SCRIPT_MODULES = {
%MAP%
}


def script_path(script: str) -> Path:
    """Путь к файлу скрипта (несуществующий путь, если скрипта нет)."""
    mod = SCRIPT_MODULES.get(script)
    if not mod:
        return ROOT / "_missing_" / script
    return ROOT / (mod.replace(".", "/") + ".py")


def script_cmd(script: str, *args, unbuffered: bool = False) -> list[str] | None:
    """Команда запуска скрипта или None, если такого скрипта нет."""
    mod = SCRIPT_MODULES.get(script)
    if not mod or not script_path(script).exists():
        return None
    cmd = [sys.executable]
    if unbuffered:
        cmd.append("-u")
    cmd += ["-m", mod]
    cmd += [str(a) for a in args]
    return cmd
'''


# ───────────────────────── утилиты ─────────────────────────
def read(path: Path) -> tuple[str, str]:
    raw = path.read_bytes().decode("utf-8")
    style = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), style


def write(path: Path, text: str, style: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", style).encode("utf-8"))


def new_rel(old_name: str) -> str:
    pkg, new = MODULES[old_name]
    return f"redcat/{pkg}/{new}.py"


# ───────────────────────── перезапись импортов ─────────────────────────
def rewrite_imports(text: str) -> tuple[str, int]:
    n = 0
    names = "|".join(sorted(MODULES, key=len, reverse=True))
    re_import_as = re.compile(rf"^(\s*)import ({names})\s+as\s+(\w+)\s*$")
    re_import = re.compile(rf"^(\s*)import ({names})\s*$")
    re_from = re.compile(rf"^(\s*)from ({names}) import (.+)$")
    out = []
    for line in text.split("\n"):
        m = re_import_as.match(line)
        if m:
            ind, mod, alias = m.groups()
            pkg, new = MODULES[mod]
            line = f"{ind}from redcat.{pkg} import {new} as {alias}"
            n += 1
        else:
            m = re_import.match(line)
            if m:
                ind, mod = m.groups()
                pkg, new = MODULES[mod]
                line = (f"{ind}from redcat.{pkg} import {new}" if new == mod
                        else f"{ind}from redcat.{pkg} import {new} as {mod}")
                n += 1
            else:
                m = re_from.match(line)
                if m:
                    ind, mod, rest = m.groups()
                    pkg, new = MODULES[mod]
                    line = f"{ind}from redcat.{pkg}.{new} import {rest}"
                    n += 1
        out.append(line)
    return "\n".join(out), n


def ensure_import(text: str, line: str) -> str:
    """Вставляет import после `from __future__` / шапки, если его ещё нет."""
    if re.search(rf"^{re.escape(line)}\s*$", text, re.M):
        return text
    lines = text.split("\n")
    idx = None
    for i, l in enumerate(lines):
        if l.startswith("import ") or l.startswith("from "):
            if "__future__" in l:
                continue
            idx = i
            break
    if idx is None:
        lines.append(line)
    else:
        lines.insert(idx, line)
    return "\n".join(lines)


# ───────────────────────── перезапись путей ─────────────────────────
def rewrite_paths(text: str, rel: str) -> tuple[str, bool]:
    orig = text
    # JSON-конфиги, лежавшие рядом с модулем
    text = re.sub(
        r'Path\(__file__\)\.resolve\(\)\.parent / "(field_semantics|hc_aliases)\.json"',
        r'paths.CONFIG_DIR / "\1.json"', text)
    text = text.replace("Path(__file__).resolve().parent", "paths.ROOT")
    # каталоги конфигурации и статики
    text = re.sub(r'\b(?:BASE_DIR|HERE|BASE|base) / "sources_external"',
                  "paths.SOURCES_EXT_DIR", text)
    text = re.sub(r'\b(?:BASE_DIR|HERE|BASE|base) / "sources"',
                  "paths.SOURCES_DIR", text)
    text = re.sub(r'\bBASE_DIR / "web"', "paths.WEB_DIR", text)
    if text != orig:
        text = ensure_import(text, "from redcat.core import paths")
    return text, text != orig


# ───────────────────────── точечные правки запуска скриптов ─────────────────────────
def patch(text: str, old: str, new: str, label: str, log: list[str]) -> str:
    if old not in text:
        log.append(f"  !! не найдено место правки: {label}")
        return text
    log.append(f"  ok {label}")
    return text.replace(old, new, 1)


def patch_runners(rel: str, text: str, log: list[str]) -> str:
    if rel.endswith("web/webapp.py"):
        text = patch(text,
            'cmd = [sys.executable, "-u", str(BASE_DIR / "redcat_scraper.py")] + args',
            'cmd = runner.script_cmd("redcat_scraper.py", *args, unbuffered=True)',
            "webapp: запуск скрапера", log)
        text = patch(text,
            'self._append("$ python " + " ".join([Path(cmd[2]).name] + cmd[3:]))',
            'self._append("$ python " + " ".join(["redcat_scraper.py"] + [str(a) for a in args]))',
            "webapp: подпись команды", log)
        text = patch(text,
            '            script = BASE_DIR / tool["script"]\n'
            '            if not script.exists():\n'
            '                return {"ok": False, "error": f"Нет файла {tool[\'script\']}"}\n'
            '            cmd = [sys.executable, "-u", str(script)] + list(tool.get("args") or [])\n',
            '            cmd = runner.script_cmd(tool["script"], *(tool.get("args") or []),\n'
            '                                    unbuffered=True)\n'
            '            if cmd is None:\n'
            '                return {"ok": False, "error": f"Нет файла {tool[\'script\']}"}\n',
            "webapp: запуск инструментов", log)
        text = ensure_import(text, "from redcat.core import runner")
    elif rel.endswith("bot/tg_bot.py"):
        text = patch(text, 'EXPORT_SCRIPT = HERE / "export_problem_jc.py"',
                     'EXPORT_SCRIPT = runner.script_path("export_problem_jc.py")',
                     "tg_bot: EXPORT_SCRIPT", log)
        text = patch(text, '[sys.executable, str(EXPORT_SCRIPT)],',
                     'runner.script_cmd("export_problem_jc.py"),',
                     "tg_bot: запуск экспорта", log)
        text = patch(text, 'SCRAPER_SCRIPT = HERE / "redcat_scraper.py"',
                     'SCRAPER_SCRIPT = runner.script_path("redcat_scraper.py")',
                     "tg_bot: SCRAPER_SCRIPT", log)
        text = patch(text, '[sys.executable, "-u", str(SCRAPER_SCRIPT), *args],',
                     'runner.script_cmd("redcat_scraper.py", *args, unbuffered=True),',
                     "tg_bot: запуск сбора", log)
        text = patch(text, '    script_path = HERE / script_name\n',
                     '    script_path = runner.script_path(script_name)\n',
                     "tg_bot: путь отчёта", log)
        text = patch(text,
                     'cmd = [sys.executable, str(script_path), "--no-open"]',
                     'cmd = runner.script_cmd(script_name, "--no-open")',
                     "tg_bot: запуск отчёта", log)
        text = ensure_import(text, "from redcat.core import runner")
    elif rel.endswith("tools/launcher.py"):
        text = patch(text, 'SCRIPT_PATH = BASE_DIR / "redcat_scraper.py"',
                     'SCRIPT_PATH = runner.script_path("redcat_scraper.py")',
                     "launcher: SCRIPT_PATH", log)
        text = patch(text,
                     '[sys.executable, str(SCRIPT_PATH), *self.run_extra_args],',
                     'runner.script_cmd("redcat_scraper.py", *self.run_extra_args),',
                     "launcher: запуск сбора", log)
        text = patch(text, 'test_path = BASE_DIR / "selftest_readonly.py"',
                     'test_path = runner.script_path("selftest_readonly.py")',
                     "launcher: путь selftest", log)
        text = patch(text, '[sys.executable, str(test_path)], cwd=str(BASE_DIR),',
                     'runner.script_cmd("selftest_readonly.py"), cwd=str(BASE_DIR),',
                     "launcher: запуск selftest", log)
        text = ensure_import(text, "from redcat.core import runner")
    elif rel.endswith("tools/match_complex_names.py"):
        text = patch(text, 'OUT_FILE = BASE / "hc_aliases.json"',
                     'OUT_FILE = paths.CONFIG_DIR / "hc_aliases.json"',
                     "match_complex_names: OUT_FILE", log)
    elif rel.endswith("tools/fill_aliases.py"):
        text = patch(text, 'OUT = BASE / "hc_aliases.json"',
                     'OUT = paths.CONFIG_DIR / "hc_aliases.json"',
                     "fill_aliases: OUT", log)
    elif rel.endswith("tools/make_field_labels.py"):
        text = patch(text, 'OUT_FILE = BASE_DIR / "field_labels.py"',
                     'OUT_FILE = BASE_DIR / "redcat" / "data" / "field_labels.py"',
                     "make_field_labels: OUT_FILE", log)
    elif rel.endswith("tools/check_runtime.py"):
        text = patch(text,
            'for f in ("redcat_scraper.py", "webapp.py", "tg_bot.py", "studio.py"):\n'
            '    print(f"  {\'OK\' if (HERE/f).exists() else \'XX\'} {f}")',
            'for f in ("redcat/collection/redcat_scraper.py", "redcat/web/webapp.py",\n'
            '          "redcat/bot/tg_bot.py", "studio.py"):\n'
            '    print(f"  {\'OK\' if (HERE/f).exists() else \'XX\'} {f}")',
            "check_runtime: список файлов", log)
    return text


BAT_FIXES = {
    "run_redcat_scraper.bat": [
        ("python redcat_scraper.py", "python -m redcat.collection.redcat_scraper")],
    "launcher.bat": [("python launcher.py", "python -m redcat.tools.launcher")],
    "check_readonly.bat": [("selftest_readonly.py", "-m redcat.tools.selftest_readonly")],
    "demo_data.bat": [("demo_data.py", "-m redcat.tools.demo_data")],
    "set_token.bat": [("set_token.py", "-m redcat.tools.set_token")],
}


def fix_usage_hints(text: str) -> str:
    def repl(m):
        mod = m.group(1)
        if mod in MODULES:
            pkg, new = MODULES[mod]
            return f"python -m redcat.{pkg}.{new}"
        return m.group(0)
    return re.sub(r"python (\w+)\.py\b", repl, text)


# ───────────────────────── основной ход ─────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply = args.apply
    log: list[str] = []

    # 0. проверка: все ли исходники на месте
    missing = [m for m in MODULES if not (ROOT / f"{m}.py").exists()]
    if missing:
        print("Не найдены модули (возможно, миграция уже применена):", missing)
        return 1

    # 1. карта скриптов для runner
    mapping = ",\n".join(
        f'    "{old}.py": "redcat.{pkg}.{new}"'
        for old, (pkg, new) in sorted(MODULES.items()))

    # 2. читаем и преобразуем все модули ДО перемещения
    plan: list[tuple[Path, str, str]] = []   # (куда, текст, стиль строк)
    for old in sorted(MODULES):
        rel = new_rel(old)
        text, style = read(ROOT / f"{old}.py")
        text, n_imp = rewrite_imports(text)
        text, _ = rewrite_paths(text, rel)
        text = patch_runners(rel, text, log)
        text = fix_usage_hints(text)
        plan.append((ROOT / rel, text, style))
        log.append(f"{old}.py -> {rel}  (импортов переписано: {n_imp})")

    for old, newrel in OTHER_MOVES.items():
        p = ROOT / old
        if not p.exists():
            continue
        if old.endswith(".py"):
            text, style = read(p)
            text, _ = rewrite_imports(text)
            text, _ = rewrite_paths(text, newrel)
            plan.append((ROOT / newrel, text, style))
        else:
            plan.append((ROOT / newrel, p.read_bytes().decode("utf-8"), "raw"))
        log.append(f"{old} -> {newrel}")

    # studio.py остаётся в корне, но импорты меняются
    st, st_style = read(ROOT / "studio.py")
    st, n = rewrite_imports(st)
    st, _ = rewrite_paths(st, "studio.py")
    plan.append((ROOT / "studio.py", st, st_style))
    log.append(f"studio.py (остаётся в корне, импортов: {n})")

    # 3. вывод плана
    print("\n".join(log))
    stale = [p.name for p in ROOT.glob("*.bak*")] + \
            [p.name for p in (ROOT / "web").glob("*.bak*")] + \
            [p.name for p in (ROOT / "web").glob("*.diff")]
    print(f"\nбудет удалено резервных копий/диффов: {len(stale)}")
    if not apply:
        print("\nDRY-RUN. Для применения: python migrate_layout.py --apply")
        return 0

    # 4. применение
    for d in PACKAGES:
        (ROOT / d).mkdir(parents=True, exist_ok=True)
        init = ROOT / d / "__init__.py"
        if not init.exists():
            init.write_text('"""Пакет. Содержимое — в подмодулях."""\n',
                            encoding="utf-8")
    (ROOT / "tests").mkdir(exist_ok=True)

    (ROOT / "redcat/core/paths.py").write_text(PATHS_PY, encoding="utf-8")
    (ROOT / "redcat/core/runner.py").write_text(
        RUNNER_PY.replace("%MAP%", mapping), encoding="utf-8")

    old_files = [ROOT / f"{m}.py" for m in MODULES] + \
                [ROOT / k for k in OTHER_MOVES if (ROOT / k).exists()]
    for dest, text, style in plan:
        if style == "raw":
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(text.encode("utf-8"))
        else:
            write(dest, text, style)
    for f in old_files:
        if f.exists() and f.resolve() not in {d.resolve() for d, _, _ in plan}:
            f.unlink()

    # каталоги источников и статика
    for src_dir, dst in DIR_MOVES.items():
        s = ROOT / src_dir
        if s.is_dir():
            (ROOT / dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(s), str(ROOT / dst))
    static = ROOT / "redcat/web/static"
    static.mkdir(parents=True, exist_ok=True)
    for name in STATIC_FILES:
        f = ROOT / "web" / name
        if f.exists():
            shutil.move(str(f), str(static / name))

    # чистка резервных копий
    for p in list(ROOT.glob("*.bak*")) + list((ROOT / "web").glob("*.bak*")) \
            + list((ROOT / "web").glob("*.diff")):
        p.unlink()
    if (ROOT / "web").is_dir() and not any((ROOT / "web").iterdir()):
        (ROOT / "web").rmdir()

    # .bat
    for bat, fixes in BAT_FIXES.items():
        p = ROOT / bat
        if p.exists():
            t, st_ = read(p)
            for a, b in fixes:
                t = t.replace(a, b)
            write(p, t, st_)
    studio_bat = ROOT / "studio.bat"
    if studio_bat.exists():
        t, st_ = read(studio_bat)
        t = t.replace("studio.py, webapp.py и папка web", "studio.py и папка redcat")
        write(studio_bat, t, st_)

    print("\nГотово. Дальше: python -m tests.test_name_normalizer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
