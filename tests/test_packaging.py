"""Метаданные дистрибутива.

Всё, что проверяется здесь, ломается тихо: пакет собирается, ставится и работает, а
недостача видна только на странице пакета или в чужом проверяющем типы. Поэтому сторожится
не поведение, а объявления в `setup.py` и наличие файлов, на которые они ссылаются.

Разбор регулярным выражением, а не импортом `setup.py`: импорт вызвал бы `setup()`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SETUP = "setup.py"


@pytest.fixture()
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture()
def setup_py(repo_root) -> str:
    return (repo_root / SETUP).read_text(encoding="utf-8")


def test_py_typed_is_declared_and_present(repo_root, setup_py):
    """Аннотации едут вместе с пакетом (PEP 561).

    Файл лежит в дереве, но в колесо попадает только по объявлению: без него у
    потребителя, проверяющего типы, аннотаций пакета просто нет.
    """
    assert (repo_root / "partest_load" / "py.typed").is_file()
    assert re.search(r"\"partest_load\"\s*:\s*\[[^\]]*py\.typed", setup_py)


def test_long_description_points_at_a_file_that_exists(repo_root, setup_py):
    """`long_description` ведёт в существующий файл, иначе не собирается и сам дистрибутив."""
    match = re.search(r"HERE\s*/\s*\"(\w+)\"\s*/\s*\"([\w.]+)\"", setup_py)

    assert match, "в setup.py нет описания для страницы пакета"
    assert (repo_root / match.group(1) / match.group(2)).is_file()
    assert 'long_description_content_type="text/markdown"' in setup_py


def test_license_is_claimed_and_shipped(repo_root, setup_py):
    """Заявленная лицензия подтверждена файлом.

    `license="MIT"` без `LICENSE` — обещание без текста: у пакета на PyPI нет условий,
    на которых его разрешено использовать.
    """
    assert 'license="MIT"' in setup_py
    assert "MIT License" in (repo_root / "LICENSE").read_text(encoding="utf-8")


def test_the_page_says_where_the_source_is(setup_py):
    """Ссылка на репозиторий — единственный путь со страницы пакета к его исходникам."""
    assert "project_urls={" in setup_py
    assert re.search(r"\"Source\"\s*:\s*\"https://github\.com/\S+\"", setup_py)


def test_the_version_lives_in_exactly_one_place(setup_py):
    """`setup.py` читает номер из пакета; второй литерал — это как они тихо разъезжаются."""
    from partest_load import __version__

    assert __version__ not in setup_py, "setup.py должен читать версию, а не повторять её"
    assert "version=version()" in setup_py


def test_the_audited_requirements_match_what_the_package_declares(repo_root, setup_py):
    """`requirements.txt` существует ради аудита и обязан совпадать с `setup.py`.

    `pip-audit` читает зависимости только из `pyproject.toml` или `requirements.txt`;
    объявленные императивно в `setup.py` он не видит и отвечает «не проверено». Это
    неотличимо от «уязвимостей нет» для того, кто читает вывод по диагонали, поэтому
    файл заведён — а раз заведён, он обязан описывать то же, что ставится.

    Список, разошедшийся с `setup.py`, хуже отсутствующего: аудит идёт, отчитывается
    зелёным и проверяет не то, что установлено.
    """
    audited = repo_root / "requirements.txt"
    assert audited.is_file(), "requirements.txt нужен pip-audit — без него аудита нет"

    def names(text: str) -> set:
        found = set()
        for raw in re.findall(r'"([A-Za-z][^"]*)"', text):
            name = re.split(r"[<>=!~\[;]", raw, 1)[0].strip().lower()
            if name and not name.startswith("partest-load"):
                found.add(name)
        return found

    def bracketed(text: str, opener: str) -> str:
        """Содержимое списка со счётом скобок.

        Непрожорливый `\[(.*?)\]` обрывается на первой закрывающей — а она внутри
        `httpx[http2]`, и половина зависимостей теряется молча.
        """
        at = text.index(opener) + len(opener) - 1
        depth = 0
        for i in range(at, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    return text[at + 1:i]
        raise AssertionError(f"не закрыт список после {opener!r}")

    declared = names(
        bracketed(setup_py, "install_requires=[")
        + bracketed(setup_py, '"report": [')
    )
    listed = {
        re.split(r"[<>=!~\[;]", line, 1)[0].strip().lower()
        for line in audited.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert declared == listed, (
        "requirements.txt разошёлся с setup.py — аудит проверяет не то, что ставится.\n"
        f"  объявлено в setup.py, но не в requirements.txt: {sorted(declared - listed)}\n"
        f"  в requirements.txt, но не объявлено: {sorted(listed - declared)}"
    )
