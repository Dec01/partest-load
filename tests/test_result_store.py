"""Хранилище результатов прогона.

Прогон длится минуты или часы, и потерять его на записи — худшее, что может сделать этот
модуль. Поэтому сторожатся: раскладка файлов, указатель на последний прогон (включая путь
без симлинков, то есть Windows), проверка имён до прогона и прореживание, которое не теряет
ошибок.

Отдельно сторожится то, чем прогон опознаётся. Личность прогона — **операция** (`operation`,
«МЕТОД /путь»), а слаг — подпись для глаз. Раскладка, ключуемая подписью, сводит с картой
только по догадке, и именно это здесь проверяется: разные подписи одной операции ложатся
вместе, одинаковые подписи разных операций — раздельно.
"""

from __future__ import annotations

import json
import random

import pytest

from partest_load.operation import operation_key
from partest_load.storage.result_store import (
    ResultStoreError,
    get_latest_run_path,
    iter_latest_runs,
    latest_pointer_path,
    run_history,
    sample_raw_results,
    save_run_results,
)
from partest_load.storage.schema import AggregatedMetrics

ITEMS = "POST /v1/items"
ITEM_BY_ID = "GET /v1/items/{id}"


def _save(root, operation=ITEMS, slug="items", profile="steady", metrics=None, **over):
    return save_run_results(root, operation=operation, slug=slug, profile_name=profile,
                            metrics=metrics if metrics is not None else _metrics(),
                            events=over.pop("events", []), **over)


def _metrics(**over) -> AggregatedMetrics:
    base = dict(total_requests=100, success_count=95, success_rate=0.95, error_rate_pct=5.0,
                real_rps=12.5, duration_sec=8.0, p50_ms=100.0, p95_ms=250.0, p99_ms=900.0,
                max_latency_ms=1200.0, mean_latency_ms=140.0, avg_response_mb=0.4,
                max_response_mb=1.1)
    base.update(over)
    return AggregatedMetrics(**base)


def _raw(i: int, ok: bool = True) -> dict:
    return {
        "timestamp": "2026-09-12T10:00:00+00:00",
        "request_start_ts": 1_700_000_000.0 + i,
        "worker_id": "w1-aaaaaaaa",
        "phase": "steady",
        "url": "https://service.invalid/v1/items",
        "method": "POST",
        "status": 200 if ok else 503,
        "duration_sec": 0.12,
        "success": ok,
        "size_mb": 0.3,
        "error_type": None if ok else "server_5xx",
    }


def test_run_lands_where_the_layout_promises(tmp_path):
    saved = _save(tmp_path)

    assert saved.path.parent == tmp_path / operation_key(ITEMS) / "steady"
    assert saved.path.name == f"{saved.run_id}.json"
    data = json.loads(saved.path.read_text(encoding="utf-8"))
    assert data["operation"] == ITEMS
    assert data["slug"] == "items"
    assert data["profile_name"] == "steady"
    assert data["format_version"] == "2.0"


def test_the_operation_reaches_the_file_itself_not_only_the_folder_name(tmp_path):
    """Операция лежит в файле в исходном виде.

    Имя каталога получается из операции необратимо (читаемый остаток плюс хеш), и если бы
    операция жила только там, читателю результата пришлось бы её угадывать — а сведение с
    картой требует ровно её, вместе с `{placeholders}` пути.
    """
    saved = _save(tmp_path, operation=ITEM_BY_ID, slug="item-read")

    data = json.loads(saved.path.read_text(encoding="utf-8"))
    assert data["operation"] == "GET /v1/items/{id}"
    # В имени каталога этой строки нет и быть не может: там нет ни пробела, ни скобок.
    assert "{id}" not in saved.path.parent.name


def test_one_operation_with_two_signatures_lands_in_one_place(tmp_path):
    """Личность прогона — операция, а не подпись.

    Один и тот же эндпоинт, названный потребителем сегодня `items`, а завтра `orders-create`,
    остаётся одним эндпоинтом: история прогонов не должна разваливаться от переименования.
    """
    first = _save(tmp_path, operation=ITEMS, slug="items")
    second = _save(tmp_path, operation=ITEMS, slug="orders-create")

    assert first.path.parent == second.path.parent
    assert len(run_history(tmp_path, ITEMS, "steady")) == 2
    assert get_latest_run_path(tmp_path, ITEMS, "steady") == second.path.resolve()
    assert len(list(iter_latest_runs(tmp_path))) == 1


def test_two_operations_with_one_signature_do_not_overwrite_each_other(tmp_path):
    """Одинаковая подпись у разных операций не сводит их в одно место.

    Подпись придумывает человек, и `items` у него легко и список, и чтение одной записи.
    Ключуй раскладку подписью — второй прогон затирает указатель первого, и в отчёте
    остаётся один эндпоинт из двух.
    """
    listing = _save(tmp_path, operation=ITEMS, slug="items")
    single = _save(tmp_path, operation=ITEM_BY_ID, slug="items")

    assert listing.path.parent != single.path.parent
    assert get_latest_run_path(tmp_path, ITEMS, "steady") == listing.path.resolve()
    assert get_latest_run_path(tmp_path, ITEM_BY_ID, "steady") == single.path.resolve()
    assert len(list(iter_latest_runs(tmp_path))) == 2


def test_latest_pointer_resolves_without_symlinks(tmp_path):
    """Указатель на последний прогон читается и там, где симлинки запрещены.

    На Windows без режима разработчика `symlink_to` бросает OSError — уже после того, как
    результат записан. Прогон, который нельзя найти, равен потерянному.
    """
    saved = _save(tmp_path)

    pointer = latest_pointer_path(tmp_path, ITEMS, "steady")
    assert pointer.exists() or pointer.is_symlink()
    assert get_latest_run_path(tmp_path, ITEMS, "steady") == saved.path.resolve()


def test_latest_pointer_follows_the_newest_run(tmp_path):
    first = _save(tmp_path)
    second = _save(tmp_path, metrics=_metrics(real_rps=99.0))

    assert first.path != second.path
    assert get_latest_run_path(tmp_path, ITEMS, "steady") == second.path.resolve()
    history = run_history(tmp_path, ITEMS, "steady")
    assert len(history) == 2
    assert history[0].name.split("__")[0] >= history[1].name.split("__")[0]


def test_iter_latest_runs_lists_every_pair(tmp_path):
    _save(tmp_path, profile="steady")
    _save(tmp_path, profile="stress")
    _save(tmp_path, operation="GET /v1/search", slug="search", profile="steady")

    pairs = {(key, profile) for key, profile, _ in iter_latest_runs(tmp_path)}

    assert pairs == {(operation_key(ITEMS), "steady"), (operation_key(ITEMS), "stress"),
                     (operation_key("GET /v1/search"), "steady")}


def test_bad_names_are_refused_before_anything_is_written(tmp_path):
    """Имя проверяется до записи.

    Схема прогона проверяет `run_id` шаблоном. Заглавная буква в подписи роняла сохранение
    уже после прогона — в момент, когда результат ещё нигде не лежит.
    """
    with pytest.raises(ResultStoreError, match="подпись эндпоинта"):
        _save(tmp_path, slug="Items")
    with pytest.raises(ResultStoreError, match="имя профиля"):
        _save(tmp_path, profile="steady profile")

    assert list(tmp_path.iterdir()) == []


def test_a_run_without_an_operation_does_not_assemble(tmp_path):
    """Операция обязательна, и её отсутствие — не «значение по умолчанию».

    Прогон, записанный «куда-нибудь», хуже незаписанного: он выглядит результатом, а с
    картой не сводится ничем.
    """
    with pytest.raises(TypeError):
        save_run_results(tmp_path, slug="items", profile_name="steady",
                         metrics=_metrics(), events=[])
    with pytest.raises(ResultStoreError, match="операция прогона"):
        _save(tmp_path, operation="")
    # Слаг операцией не является, даже если очень похож на имя эндпоинта.
    with pytest.raises(ResultStoreError, match="операция прогона"):
        _save(tmp_path, operation="orders-create")
    # Метод без пути и путь без метода — тоже не операция.
    with pytest.raises(ResultStoreError, match="операция прогона"):
        _save(tmp_path, operation="/v1/items")
    with pytest.raises(ResultStoreError, match="операция прогона"):
        _save(tmp_path, operation="GET")

    assert list(tmp_path.iterdir()) == []


def test_raw_results_are_saved_only_when_asked(tmp_path):
    raw = [_raw(i) for i in range(5)]

    without = _save(tmp_path, raw_results=raw, save_raw=False)
    assert "raw_results" not in json.loads(without.path.read_text(encoding="utf-8"))

    with_raw = _save(tmp_path, raw_results=raw, save_raw=True)
    stored = json.loads(with_raw.path.read_text(encoding="utf-8"))
    assert len(stored["raw_results"]) == 5


def test_unknown_fields_of_a_raw_result_survive_saving(tmp_path):
    """Воркер дописывает к результату поля, которых схема не знает: их нельзя терять.

    `size_range` не объявлен в модели, но именно по нему отчёт строит разбивку по размеру
    ответа. Запрет лишних полей выкинул бы её молча.
    """
    raw = [{**_raw(0), "size_range": "1–3 MB"}]

    saved = _save(tmp_path, raw_results=raw, save_raw=True)

    stored = json.loads(saved.path.read_text(encoding="utf-8"))
    assert stored["raw_results"][0]["size_range"] == "1–3 MB"


def test_no_temporary_files_are_left_behind(tmp_path):
    saved = _save(tmp_path)

    assert [p.name for p in saved.path.parent.iterdir()] == [saved.path.name]


def test_sampling_keeps_every_error(tmp_path):
    """Прореживание длинного прогона не теряет ни одной ошибки.

    Ради ошибок сырые результаты и открывают. Равномерная выборка оставила бы примерно
    пятую часть и их — а искать в отчёте ошибку, которой в файле нет, нельзя.
    """
    results = [_raw(i, ok=(i % 10 != 0)) for i in range(100)]
    errors = [r for r in results if not r["success"]]

    kept = sample_raw_results(results, threshold=10, success_rate=0.2,
                              rng=random.Random(20260912))

    assert len(errors) == 10
    assert all(error in kept for error in errors)
    assert len(kept) < len(results)


def test_short_run_is_not_sampled_at_all():
    results = [_raw(i) for i in range(30)]

    assert sample_raw_results(results, threshold=50) == results


def test_metrics_survive_the_round_trip(tmp_path):
    metrics = _metrics(saturation_point_concurrency=240, time_to_first_error_sec=42.5,
                       degradation_slope_pct_per_min=18.2, degradation_status="moderate")

    saved = _save(tmp_path, profile="stress", metrics=metrics)

    stored = json.loads(saved.path.read_text(encoding="utf-8"))["metrics"]
    assert stored["saturation_point_concurrency"] == 240
    assert stored["time_to_first_error_sec"] == pytest.approx(42.5)
    assert stored["degradation_status"] == "moderate"
    # Считается, а не хранится: 5% ошибок при бюджете 5% — бюджет израсходован целиком.
    assert stored["error_budget_burned_pct"] == pytest.approx(100.0)
