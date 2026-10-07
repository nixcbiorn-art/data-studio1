#!/usr/bin/env python3
"""check_runtime.py — диагностика окружения."""
from redcat.core import paths
import importlib.util, sys
from pathlib import Path
HERE = paths.ROOT
print("=" * 70); print("  ДИАГНОСТИКА ОКРУЖЕНИЯ"); print("=" * 70)
print(f"\n▸ PYTHON: {sys.executable}")
print(f"  version: {sys.version.split()[0]}  platform: {sys.platform}")
print(f"\n▸ ПАПКА: {HERE}")
for f in ("redcat/collection/redcat_scraper.py", "redcat/web/webapp.py",
          "redcat/bot/tg_bot.py", "studio.py"):
    print(f"  {'OK' if (HERE/f).exists() else 'XX'} {f}")
print("\n▸ МОДУЛИ:")
for name in ("requests", "aiohttp", "pandas", "dotenv", "playwright"):
    print(f"  {'OK' if importlib.util.find_spec(name) else 'XX'} {name}")
env = HERE / ".env"
print("\n▸ .env:")
if not env.exists():
    print(f"  XX нет: {env}")
else:
    for ln in env.read_text(encoding="utf-8", errors="replace").splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s: continue
        k, v = s.split("=", 1); v = v.strip().strip('"').strip("'")
        prev = v[:4] + "..." if len(v) > 4 else (v or "(пусто)")
        print(f"  {'OK' if v else '!!'} {k.strip()} = {prev}")
print("\n" + "=" * 70)
