"""Операция прогона и её ключ в имени файла.

Ключ операции — интерфейс: по нему складывается раскладка каталогов и имена указателей, и
сменить его правило после первого прогона значит потерять всю историю. Поэтому здесь
сторожится не «функция работает», а три свойства: ключ годен для имени файла, одна операция
всегда даёт один ключ, разные операции — разные ключи.
"""

from __future__ import annotations

import pytest

from partest_load.operation import check_operation, normalize_operation, operation_key

# Две операции, читаемые части которых после упрощения совпадают: различает их только хеш.
TWINS = ("GET /v1/items/{id}", "GET /v1/items/{key}")


def test_the_operation_is_method_and_path():
    assert normalize_operation("get", "v1/items") == "GET /v1/items"
    assert normalize_operation("GET", "/v1/items") == "GET /v1/items"
    assert normalize_operation("post", "") == "POST /"
    # Шаблон пути не переписывается: `{id}` — часть опознания эндпоинта, а не значение.
    assert normalize_operation("GET", "/v1/items/{id}") == "GET /v1/items/{id}"


def test_what_is_not_an_operation_is_refused():
    for bad in ["", "orders-create", "/v1/items", "GET", "GET v1/items",
                "FETCH /v1/items", "GET /v1/items?page=1", "GET /v1/items#top",
                "GET /v1/bad path", None]:
        with pytest.raises(ValueError, match="операция прогона"):
            check_operation(bad)


def test_the_key_is_safe_for_a_file_name():
    key = operation_key("GET /v1/items/{id}")

    assert key.replace("-", "").isalnum()
    assert key.islower()
    # Шаблон идентификатора прогона проверяется схемой: ключ обязан в него укладываться.
    assert all(c.isalnum() or c in "-_" for c in key)


def test_the_key_is_readable_and_stable():
    """Читаемая часть нужна человеку, смотрящему в каталог результатов."""
    key = operation_key("GET /v1/items")

    assert key.startswith("get-v1-items-")
    assert key == operation_key("GET /v1/items")


def test_the_key_survives_a_path_longer_than_a_file_name():
    """Длинный путь не даёт имени каталога расти без предела — и не теряет различимости."""
    long_one = "GET /" + "/".join(f"segment{i}" for i in range(30))
    other = long_one + "/more"

    assert len(operation_key(long_one)) < 64
    assert operation_key(long_one) != operation_key(other)


def test_different_operations_get_different_keys():
    first, second = TWINS

    assert operation_key(first) != operation_key(second)
    # Метод — тоже часть личности: читать и создавать по одному пути это разные узлы.
    assert operation_key("GET /v1/items") != operation_key("POST /v1/items")
