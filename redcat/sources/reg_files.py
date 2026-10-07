"""reg_files — чтение, запись и удаление файлов описаний источников."""
from __future__ import annotations

import json
from pathlib import Path
from redcat.sources.reg_core import _spec_from_dict


def list_source_files(directory) -> list:
    directory = Path(directory)
    out = []
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            out.append((path, "⚠️ ошибка чтения", "", str(path)))
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            out.append((path, item.get("key", "?"), item.get("title", ""),
                        item.get("url", "")))
    return out


def save_source_file(directory, spec_dict: dict):
    directory = Path(directory)
    directory.mkdir(exist_ok=True)
    key = spec_dict.get("key") or "new_source"
    _spec_from_dict(spec_dict)
    path = directory / f"{key}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(spec_dict, f, ensure_ascii=False, indent=2)
    return path


def delete_source_file(path) -> None:
    Path(path).unlink(missing_ok=True)
