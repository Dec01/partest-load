"""Метаданные дистрибутива.

Всё, что проверяется здесь, ломается тихо: пакет собирается, ставится и работает, а
недостача видна только на странице пакета или в чужом проверяющем типы. Поэтому сторожится
не поведение, а объявления в `pyproject.toml` и наличие файлов, на которые они ссылаются.

Разбор регулярным выражением, а не `tomllib`: он появился в 3.11, а пакет заявляет 3.10.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PYPROJECT = "pyproject.toml"


@pytest.fixture()
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture()
def pyproject(repo_root) -> str:
    return (repo_root / PYPROJECT).read_text(encoding="utf-8")


def test_py_typed_is_declared_and_present(repo_root, pyproject):
    """Аннотации едут вместе с пакетом (PEP 561).

    Файл лежит в дереве, но в колесо попадает только по объявлению: без него у
    потребителя, проверяющего типы, аннотаций пакета просто нет.
    """
    assert (repo_root / "partest_load" / "py.typed").is_file()
    assert re.search(r"partest_load\s*=\s*\[[^\]]*py\.typed", pyproject)


def test_long_description_points_at_a_file_that_exists(repo_root, pyproject):
    """`readme` ведёт в существующий файл, иначе не собирается и сам дистрибутив."""
    match = re.search(r"^readme\s*=\s*\{\s*file\s*=\s*\"([^\"]+)\"", pyproject, re.MULTILINE)

    assert match, "в pyproject.toml нет описания для страницы пакета"
    assert (repo_root / match.group(1)).is_file()
    assert 'content-type = "text/markdown"' in match.string[match.start():match.end() + 60]


def test_license_is_claimed_and_shipped(repo_root, pyproject):
    """Заявленная лицензия подтверждена файлом.

    `license = { text = "MIT" }` без `LICENSE` — обещание без текста: у пакета на PyPI
    нет условий, на которых его разрешено использовать.
    """
    assert 'license = { text = "MIT" }' in pyproject
    assert "MIT License" in (repo_root / "LICENSE").read_text(encoding="utf-8")


def test_the_page_says_where_the_source_is(pyproject):
    """Ссылка на репозиторий — единственный путь со страницы пакета к его исходникам."""
    assert "[project.urls]" in pyproject
    assert re.search(r"^Source\s*=\s*\"https://github\.com/\S+\"", pyproject, re.MULTILINE)
