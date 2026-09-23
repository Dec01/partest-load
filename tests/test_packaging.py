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

# Опции `pytest.ini`, которые принадлежат не pytest, а плагину. Конфигурация, объявляющая
# опцию плагина, которого установка не ставит, — это предупреждение «Unknown config option»
# в каждом прогоне и тихо неработающая настройка. Появилась новая опция чужого плагина —
# строка сюда, иначе сторож её не увидит.
INI_OPTION_OWNERS = {
    "asyncio_": "pytest-asyncio",
    "benchmark": "pytest-benchmark",
    "timeout": "pytest-timeout",
}


def bracketed(text: str, opener: str) -> str:
    """Содержимое списка со счётом скобок.

    Непрожорливый `\\[(.*?)\\]` обрывается на первой закрывающей — а она внутри
    `httpx[http2]`, и половина зависимостей теряется молча.
    """
    assert opener in text, f"в setup.py нет списка {opener!r}"
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


def names(text: str) -> set:
    """Имена дистрибутивов из куска `setup.py`, без версий, extra и собственных ссылок."""
    found = set()
    for raw in re.findall(r'"([A-Za-z][^"]*)"', text):
        name = re.split(r"[<>=!~\[;]", raw, maxsplit=1)[0].strip().lower()
        if name and not name.startswith("partest-load"):
            found.add(name)
    return found


def requirements(lines) -> set:
    """Пары «имя с extra» → «границы» — то есть требование целиком, а не одно имя.

    Сверка по именам пропускает ровно то, ради чего файл и заведён: список, где имена
    те же, а пол ниже объявленного, проходит проверку и уводит аудит на версии, которых
    у потребителя не будет. По той же причине extra входит в имя: `httpx` и
    `httpx[http2]` — разный состав установки (второй тянет `h2`), и подмена одного
    другим вычёркивает пакет из аудита целиком.
    """
    found = set()
    for raw in lines:
        text = raw.split(";", 1)[0].strip()
        match = re.match(r"^([A-Za-z][A-Za-z0-9._\-]*)(\[[^\]]*\])?\s*(.*)$", text)
        if not match:
            continue
        name = match.group(1).lower()
        if name.startswith("partest-load"):
            continue
        extras = "".join(sorted(match.group(2)[1:-1].lower().split(","))) if match.group(2) else ""
        found.add((f"{name}[{extras}]" if extras else name, match.group(3).replace(" ", "")))
    return found


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

    Сверяются **имена вместе с границами**. Сверка по одним именам пропускает подмену
    пола: `pydantic>=2.0.0` в одном файле и `pydantic>=2.4.0` в другом — это один и тот
    же набор имён и два разных ответа на вопрос, что получит потребитель.
    """
    audited = repo_root / "requirements.txt"
    assert audited.is_file(), "requirements.txt нужен pip-audit — без него аудита нет"

    declared = requirements(
        re.findall(
            r'"([A-Za-z][^"]*)"',
            bracketed(setup_py, "install_requires=[")
            + bracketed(setup_py, '"report": ['),
        )
    )
    listed = requirements(
        line for line in audited.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )

    assert declared == listed, (
        "requirements.txt разошёлся с setup.py — аудит проверяет не то, что ставится.\n"
        f"  объявлено в setup.py, но не в requirements.txt: {sorted(declared - listed)}\n"
        f"  в requirements.txt, но не объявлено: {sorted(listed - declared)}"
    )


def test_the_audit_guard_notices_a_lowered_floor_and_not_only_a_missing_name():
    """Сторож обязан ловить расхождение границ, а не только имён.

    Проверка на самом сторо́же, а не на файлах: расхождение в рабочей копии завести
    нельзя — оно тут же уронит тест выше. Случай подлинный: пол `pydantic` поднимался с
    `2.0.0` до `2.4.0`, и сверка по одним именам пропустила бы `requirements.txt`,
    оставшийся на старом поле. Аудит после такого зелен и проверяет версии, которых у
    потребителя не будет.
    """
    setup_side = requirements(["pydantic>=2.4.0"])
    audit_side = requirements(["pydantic>=2.0.0"])

    assert setup_side != audit_side, "сторож не видит разницы в границах"
    assert {name for name, _ in setup_side} == {name for name, _ in audit_side}, (
        "случай подобран негодно: имена обязаны совпадать, иначе ловится не то"
    )

    # Extra входит в имя: состав установки у `httpx` и `httpx[http2]` разный.
    assert requirements(["httpx>=0.27.2"]) != requirements(["httpx[http2]>=0.27.2"])
    # А порядок extra и пробелы — нет: это запись, а не смысл.
    assert requirements(["httpx[http2] >= 0.27.2"]) == requirements(["httpx[http2]>=0.27.2"])


def test_every_extra_we_ask_for_has_a_declared_floor(repo_root, setup_py):
    """Пакет, который тянет выбранное нами extra, обязан иметь объявленную границу.

    `httpx[http2]` тянет `h2`, а границу его не объявлял никто: httpx просит `h2>=3,<5`,
    и по объявленному полу это `h2==3.0.0` — две записи PYSEC. В `requirements.txt`
    такого имени не было вовсе, то есть аудит его не видел ни разу и молчал не потому,
    что чисто.

    Правило общее: просим extra — объявляем и то, что оно приводит. Иначе пол выбирает
    чужой пакет, а отвечаем за него мы.
    """
    install = bracketed(setup_py, "install_requires=[")
    audited = (repo_root / "requirements.txt").read_text(encoding="utf-8")

    assert "httpx[http2]" in install, "клиент создаётся с http2=True — extra обязателен"
    assert re.search(r'"h2>=[\d.]+"', install), (
        "extra http2 тянет h2, а его пол не объявлен: границу выбирает httpx, "
        "и по его собственной границе это уязвимая 3.0.0"
    )
    assert re.search(r"^h2>=[\d.]+", audited, re.MULTILINE), (
        "h2 нет в requirements.txt — значит, аудит его не проверяет"
    )


def test_the_dev_extra_installs_what_the_test_run_needs(setup_py):
    """`pip install -e ".[dev]"` обязан давать окружение, в котором проходит весь набор.

    Extra `dev` не было вовсе, и это не давало ни ошибки, ни предупреждения: `pip` на
    несуществующий extra ругается одной строкой и ставит пакет без него. Дальше
    шестнадцать проверок отчёта пропускались по `importorskip`, разбор командной строки
    уходил в ветку «отчёт собрать нечем», а проверка закрытого контура искала внешние
    адреса в файле, в который нечего было вшивать. Прогон при этом оставался зелёным.

    Проверяется не список целиком — он растёт, — а то, что установка покрывает
    **объявленное в конфигурации и импортируемое тестами**. В `requirements.txt` ничего
    из `dev` не попадает намеренно: тот файл описывает, что пакет тянет у потребителя, и
    аудит проверяет именно это.
    """
    declared = bracketed(setup_py, '"dev": [')
    dev = names(declared)

    assert "pytest" in dev, "набор тестов не ставится тем, что объявлено в dev"

    ini = (Path(__file__).resolve().parent.parent / "pytest.ini").read_text(encoding="utf-8")
    options = {line.split("=", 1)[0].strip()
               for line in ini.splitlines()
               if "=" in line and not line.lstrip().startswith("#")}
    for option in sorted(options):
        for prefix, owner in INI_OPTION_OWNERS.items():
            if option.startswith(prefix):
                assert owner in dev, (
                    f"pytest.ini объявляет {option!r} — опцию плагина {owner}, "
                    "а dev его не ставит: настройка молча не работает"
                )

    # Отчёт в тестах не необязателен: без него проверка закрытого контура пуста.
    assert "partest-load[report]" in declared, (
        "dev обязан тянуть extra report: тесты отчёта импортируют jinja2/pandas, "
        "а проверка автономности читает вшитый plotly.js"
    )
