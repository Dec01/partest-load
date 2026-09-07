---
title: Журнал операций над документацией
status: current
verified: 2026-09-07
sources: []
audience: maintainer
allow_version_literals: true
---

# Журнал

Хронология: что сделано с документацией и почему. Новые записи сверху.
Формат строки: дата · операция (ingest / update / lint / restructure) · что затронуто.

## 2026-09-07 · restructure · заведена вики

Репозиторий поставлен под git и получил документацию по общей схеме семейства (скилл
`orchestra-wiki`, канон — `partest_client/docs/wiki/WIKI.md`).

Содержание взято из существовавших `AGENTS.md`, `README.md`, `CHANGELOG.md` и `pyproject.toml`.
Ничего не придумано заново.

| Страница | Откуда содержание |
|---|---|
| [[concepts/time-based-load]] | `AGENTS.md` и `README.md`, раздел «Что делает» |
| [[components/overview]] | `README.md`, раздел «Устройство», плюс `pyproject.toml` |
| [[decisions/reuse-partest]] | `README.md`, раздел «Отношение к partest», плюс `CHANGELOG.md` |
| [[status]] | `CHANGELOG.md` плюс сверка с рабочим деревом |

Сверка с рабочим деревом дала расхождение, зафиксированное в [[status]]: `README.md`
описывает раскладку `configs/`, `core/`, `engines/`, `storage/`, `reporting/`, а в дереве
есть только `__init__.py`. Документ описывает замысел, а не состояние — это допустимо для
`README`, но недопустимо для страницы `components/`, поэтому [[components/overview]] говорит
о наличии прямо.

Там же отмечен дефект: `project.scripts` объявляет команду `partest-load`, модуля
`partest_load/cli.py` нет.
