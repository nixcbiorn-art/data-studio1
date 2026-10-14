"""Characterization-тесты ядра этого цикла правок:

- api_guard.check_method — сам инвариант «только чтение» (in-process,
  без monkey-patching библиотек);
- scrape_dedup.dedup_rows — дедупликация фида;
- quality.coverage._pass_filters / coverage_pair — сверка покрытия
  (в т.ч. регрессия «op: in берёт ключ values, а не value»);
- quality.feed_health.check — детектор «пропал/заморожен».

Тесты не трогают сеть: check_method поднимает исключение до открытия
соединения, остальные функции — чистые.
"""
import types

import pytest

from redcat.core import api_guard
from redcat.collection.scrape_dedup import dedup_rows, row_id
from redcat.quality.coverage import _pass_filters, coverage_pair
from redcat.quality import feed_health


# ── api_guard ─────────────────────────────────────────────────────────
def test_guard_blocks_write_methods_on_untrusted_host():
    for m in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(api_guard.WriteRequestBlocked):
            api_guard.check_method(m, "https://api.redcat.ai/api/v1/objects")


def test_guard_allows_safe_methods():
    for m in ("GET", "HEAD", "OPTIONS"):
        assert api_guard.check_method(m, "https://api.redcat.ai/x") == m


def test_guard_telegram_post_carveout():
    # POST в Telegram разрешён по дизайну (allowlist хостов)
    assert api_guard.check_method(
        "POST", "https://api.telegram.org/bot123:abc/sendMessage") == "POST"


# ── scrape_dedup ──────────────────────────────────────────────────────
SPEC = types.SimpleNamespace(id_field="external_id")


def test_dedup_keeps_newest_by_timestamp():
    rows = [
        {"external_id": "1", "last_update_date": "2026-01-01", "v": "old"},
        {"external_id": "1", "last_update_date": "2026-05-05", "v": "new"},
    ]
    kept, removed, examples = dedup_rows(rows, SPEC)
    assert removed == 1 and examples == ["1"]
    assert len(kept) == 1 and kept[0]["v"] == "new"


def test_dedup_no_ts_fields_keeps_last_in_feed():
    rows = [{"external_id": "1", "v": "a"}, {"external_id": "1", "v": "b"}]
    kept, removed, _ = dedup_rows(rows, SPEC)
    assert removed == 1 and kept[0]["v"] == "b"


def test_dedup_preserves_rows_without_id():
    rows = [
        {"external_id": "1"}, {"external_id": "1"},
        {"external_id": None}, {"note": "без поля id"},
    ]
    kept, removed, _ = dedup_rows(rows, SPEC)
    assert removed == 1 and len(kept) == 3


def test_row_id_normalizes_numeric_forms():
    assert row_id({"id": 535}, "id") == "535"
    assert row_id({"id": "535.0"}, "id") == "535"
    assert row_id({"id": "  "}, "id") is None
    assert row_id({"id": "null"}, "id") is None


# ── quality.coverage ──────────────────────────────────────────────────
def test_pass_filters_in_uses_values_key():
    """Регрессия C2: в боевых конфигах «in» хранит список в ключе values."""
    f = [{"field": "status", "op": "in",
          "values": ["Аккредитация МК", "Срок МК"]}]
    assert _pass_filters({"status": "Срок МК"}, f) is True
    assert _pass_filters({"status": "Архив"}, f) is False


def test_pass_filters_eq_and_missing_field():
    f = {"field": "tier", "op": "eq", "value": "Комфорт"}
    assert _pass_filters({"tier": " комфорт "}, f) is True
    assert _pass_filters({}, f) is False       # пустое поле не проходит


def test_coverage_pair_counts_and_pct():
    left = [{"name": "Невский 1"}, {"name": "Невский 1"}, {"name": "Другой"}]
    right = [{"complex": "Невский 1"}, {"complex": "Третий"}]
    cc = {"on_left": "name", "on_right": "complex"}
    res = coverage_pair(left, right, cc)
    assert res["rows_right_total"] == 2
    assert res["rows_right_matched"] == 1
    assert res["coverage_right_pct"] == 50.0
    assert res["rows_left_total"] == 3
    assert res["rows_left_matched"] == 2
    assert res["coverage_left_pct"] == pytest.approx(66.7)


def test_coverage_pair_empty_right_is_none_not_crash():
    res = coverage_pair([{"name": "X"}], [], {"on_left": "name",
                                              "on_right": "name"})
    assert res["coverage_right_pct"] is None


# ── quality.feed_health ───────────────────────────────────────────────
def test_feed_health_detects_frozen_volume():
    runs = [
        {"x_count": 10, "other_count": 1, "changes_total": 5},
        {"x_count": 12, "other_count": 1, "changes_total": 5},
        {"x_count": 15, "other_count": 2, "changes_total": 5},
        {"x_count": 15, "other_count": 3, "changes_total": 5},
        {"x_count": 15, "other_count": 4, "changes_total": 5},
    ]
    states = {d["source"]: d for d in feed_health.check(runs)}
    assert states["x"]["state"] == feed_health.STATE_FROZEN


def test_feed_health_detects_missing_tail():
    runs = [
        {"x_count": 10, "other_count": 1},
        {"x_count": 9,  "other_count": 2},
        {"other_count": 3},
        {"other_count": 4},
    ]
    states = {d["source"]: d for d in feed_health.check(runs)}
    assert states["x"]["state"] == feed_health.STATE_MISSING
    assert states["x"]["tail_missing"] == 2


def test_feed_health_empty_history():
    assert feed_health.check([]) == []
