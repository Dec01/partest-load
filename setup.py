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
        "partest>=1.8",
        # Именно с extra http2: клиент создаётся с http2=True, и без пакета `h2` httpx бросает
        # ImportError при создании клиента — то есть ни один воркер не отправляет ни одного
        # запроса. Перевести клиент на HTTP/1.1 было бы тише, но это другие измерения: у
        # мультиплексирования соединений своя кривая насыщения.
        "httpx[http2]>=0.27.2",
        "PyYAML>=6.0.2",
        "pydantic>=2.0.0",
    ],
    extras_require={
        # Дашборд не нужен тому, кто снимает только артефакт, — а numpy/pandas/plotly
        # весят больше, чем всё остальное вместе.
        "report": ["numpy>=1.24", "pandas>=2.0", "plotly>=5.18", "Jinja2>=3.1"],
        "all": ["partest-load[report]"],
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
