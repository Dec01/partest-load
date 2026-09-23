import pathlib
import re

from setuptools import find_packages, setup

HERE = pathlib.Path(__file__).parent


def version():
    """Single source of truth: partest_load/__init__.py. Never duplicate the number here."""
    text = (HERE / "partest_load" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise RuntimeError("cannot find __version__ in partest_load/__init__.py")
    return match.group(1)


def readme():
    """PyPI long_description. Kept short on purpose — the repository holds the detail."""
    return (HERE / "docs" / "PYPI.md").read_text(encoding="utf-8")


setup(
    name="partest-load",
    version=version(),
    author="dec01",
    author_email="parshin.ewgeniy@yandex.ru",
    license="MIT",
    description=(
        "Time-based load harness for the partest family: duration-driven profiles, "
        "saturation and degradation metrics, offline HTML dashboard"
    ),
    # Описание для PyPI — отдельный файл, как у `partest` и `partest-gen`. README обращён к
    # репозиторию: он говорит «пакета на PyPI пока нет» и отсылает к разделам вики, которых на
    # странице пакета не будет. Страница дистрибутива правится независимо от README.
    long_description=readme(),
    long_description_content_type="text/markdown",
    url="https://github.com/Dec01/partest-load",
    # Перечислением, а не исключением: в дистрибутив уходит ровно `partest_load` и его
    # подпакеты, и новый каталог в корне репозитория не попадёт в колесо по недосмотру.
    packages=find_packages(include=["partest_load*"]),
    include_package_data=True,
    package_data={
        # Шаблоны дашборда — данные, а не код, и сами в колесо не попадают. Без этой строки
        # пакет устанавливается целиком, а `generate_dashboard` падает на отсутствующем
        # шаблоне уже у потребителя — после прогона, то есть в самый дорогой момент.
        # `py.typed` — по той же причине: файл лежит в дереве с самого начала, но без
        # объявления в колесо не попадает, и аннотации пакета для проверяющего типы просто
        # не существуют (PEP 561).
        "partest_load": ["py.typed", "reporting/templates/*.j2"],
    },
    install_requires=[
        # Нижняя граница — старшая опубликованная линия семейства, а не самая старая
        # работающая. `>=1.8` пускал линию до слома 2.0, на которой здесь не прогоняется
        # ничего, и вместе с ней её собственные полы зависимостей.
        # Не «код требует нового», а защита потребителя: партии 2.0.x объявляют
        # уязвимые полы requests и python-dotenv, и на минимальном разрешении они
        # приезжают сюда транзитивно. 2.1.0 — первая, где они подняты.
        "partest>=2.1.0",
        # Именно с extra http2: клиент создаётся с http2=True, и без пакета `h2` httpx бросает
        # ImportError при создании клиента — то есть ни один воркер не отправляет ни одного
        # запроса. Перевести клиент на HTTP/1.1 было бы тише, но это другие измерения: у
        # мультиплексирования соединений своя кривая насыщения.
        "httpx[http2]>=0.27.2",
        # Пол для `h2` объявлен здесь, хотя тянет его extra `http2`: httpx требует `h2>=3,<5`,
        # и по объявленному полу это `h2==3.0.0` — PYSEC-2026-1435 и PYSEC-2026-3628.
        # Зависимость, которую просим мы (extra выбран нами), но границу которой не объявлял
        # никто, аудитом не проверялась вовсе: в `requirements.txt` её не было.
        "h2>=4.4.1",
        "PyYAML>=6.0.2",
        # PYSEC-2026-1812 исправлен в 2.4.0. Ниже — уязвимый пол, который резолвер обязан
        # считать допустимым.
        "pydantic>=2.4.0",
    ],
    extras_require={
        # Дашборд не нужен тому, кто снимает только артефакт, — а numpy/pandas/plotly
        # весят больше, чем всё остальное вместе.
        # Пол `Jinja2` — 3.1.6: пять записей PYSEC-2026-1471…1475 закрываются по частям
        # (3.1.3, 3.1.4, 3.1.5), и последняя из них — только 3.1.6. Здесь это не абстракция:
        # дашборд рисуется из шаблонов именно этим пакетом.
        #
        # Пол `pandas` — 2.1: не безопасность, а правда. `charts.py` зовёт `Index.round`,
        # которого в 2.0 нет вовсе, и на объявленном поле девять проверок отчёта падали с
        # `AttributeError`. Граница обещала то, чего не было; проверено установкой всего
        # объявленного ровно по полам.
        "report": ["numpy>=1.24", "pandas>=2.1", "plotly>=5.18", "Jinja2>=3.1.6"],
        "all": ["partest-load[report]"],
        # Что нужно набору тестов сверх самого пакета. Extra не было вовсе: команда
        # `pip install -e ".[dev]"` молча не ставила ничего, шестнадцать проверок отчёта
        # пропускались, а разбор командной строки уходил в ветку «отчёт собрать нечем».
        #
        # * pytest-asyncio — его требует `pytest.ini` (`asyncio_default_fixture_loop_scope`).
        #   Сами тесты зовут `asyncio.run`, но конфигурация, объявляющая опцию чужого
        #   плагина, которого установка не ставит, — это предупреждение в каждом прогоне;
        # * `partest-load[report]` — тесты отчёта импортируют jinja2, pandas и numpy, а
        #   проверка закрытого контура читает вшитый plotly.js. Без plotly она искала
        #   внешние адреса в пустоте и два месяца ничего не проверяла, оставаясь зелёной.
        #
        # Верхней границы у `pytest` здесь нет намеренно. Потолок ставят тому, кто
        # регистрирует плагин: падение регистрации убивает весь прогон потребителя. У
        # `partest-load` в `entry_points` только консольная команда, группы `pytest11` нет,
        # и `pytest` живёт лишь в этом дополнении — то есть в нашем собственном прогоне.
        # Сломается он на будущей мажорной версии — упадёт наш набор, громко и у нас, а не
        # чужой. Сверх того потолок и так приходит: `pytest-asyncio` с 1.3.0 требует
        # `pytest<10`, и второй такой же в двух местах пришлось бы поднимать дважды.
        "dev": [
            # PYSEC-2026-1845. В ветке 8.x исправления не существует — только 9.0.3.
            "pytest>=9.0.3",
            # Не безопасность, а разрешимость: все версии до 1.3.0 объявляют `pytest<9`.
            # Пара «pytest>=9.0.3 + pytest-asyncio>=0.23.7» неразрешима вовсе, а до подъёма
            # pytest резолвер молча утаскивал прогон обратно на восьмёрку.
            "pytest-asyncio>=1.3.0",
            "partest-load[report]",
        ],
    },
    entry_points={
        "console_scripts": [
            "partest-load=partest_load.cli:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Topic :: Software Development :: Testing",
        "Topic :: Software Development :: Testing :: Traffic Generation",
    ],
    # Через запятую, а не через пробел, как у соседей: два ключевых слова здесь состоят из
    # двух слов («load testing», «stress testing»), и по пробелу они распались бы на части.
    keywords="load testing, stress testing, performance, api, openapi, partest",
    project_urls={
        "Source": "https://github.com/Dec01/partest-load",
        "Issues": "https://github.com/Dec01/partest-load/issues",
        "Changelog": "https://github.com/Dec01/partest-load/blob/master/CHANGELOG.md",
        "Documentation": "https://github.com/Dec01/partest-load/blob/master/docs/wiki/index.md",
        "PyPI": "https://pypi.org/project/partest-load/",
        "partest": "https://github.com/Dec01/partest",
    },
    python_requires=">=3.10",
)
