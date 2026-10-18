"""Точка входа `partest-load`.

Здесь и только здесь пакету разрешено разговаривать с человеком: печатать сводку, настраивать
логирование и возвращать код возврата. Библиотечные модули этого не делают — под нагрузкой
печать нечем ни выключить, ни перенаправить, а `sys.exit` из библиотеки уносит с собой
чужой процесс.

Три отличия от прототипа, каждое вынужденное.

**Все пути приходят аргументами.** Прототип искал профиль в `configs/profiles` относительно
текущего каталога, перебирая три варианта имени файла, а эндпоинт импортировал из модуля
`configs.endpoints` — то есть из кода с эндпоинтами заказчика. Установленный пакет не знает
ни того, откуда его запустили, ни того, где потребитель держит свою конфигурацию.

**Нагрузка идёт только по явной команде.** Ни один импорт, ни один разбор аргументов ничего
не отправляет: сначала разбор и проверка конфигурации, и только потом, если не задан
`--dry-run`, собственно прогон.

**Ошибка — это код возврата и одна строка, а не трассировка.** Трассировка остаётся за
`--debug`: на чужой машине она не помогает, а мешает увидеть, что именно не так.

**Операция прогона отдельным аргументом не спрашивается.** Она выводится из описания
эндпоинта — метод и путь там уже есть, — и потому не может разойтись с тем, что реально
нагружали. `--endpoint` остаётся ключом в файле эндпоинтов и подписью прогона для глаз;
личность прогона — операция.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import warnings
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .configs.endpoints import (
    ConfigKeyWarning,
    EndpointConfig,
    load_endpoints,
    _bare_base,
)
from .engines import ENGINES
from .configs.profiles import ProfileError, load_profile
from .core.runner import run_load_test
from .storage.result_store import (
    ResultStoreError,
    get_latest_run_path,
    save_run_results,
    validate_run_names,
)
from .storage.schema import AggregatedMetrics

log = logging.getLogger("partest_load")

EXIT_OK = 0
EXIT_ERROR = 1
# Прогон прерван с клавиатуры. Отдельный код: это не отказ инструмента, и в сценарии
# вызывающего это разные случаи.
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="partest-load",
        description="Нагрузочный прогон по профилю: время, метрики насыщения и деградации.",
    )
    # Обязательны для прогона, но не для `--report-only`: отчёт собирается по тому, что уже
    # лежит в каталоге результатов. В прототипе те же аргументы были объявлены обязательными
    # всегда, и режим «только отчёт» нельзя было вызвать, не назвав эндпоинт и профиль.
    parser.add_argument("--endpoints", type=Path, metavar="FILE",
                        help="YAML с описаниями эндпоинтов (ключ → описание)")
    parser.add_argument("--endpoint", metavar="KEY",
                        help="Ключ эндпоинта в файле --endpoints")
    parser.add_argument("--profile", type=Path, metavar="FILE",
                        help="YAML профиля нагрузки")
    parser.add_argument("--profile-name", metavar="NAME",
                        help="Имя профиля в именах файлов результатов; "
                             "по умолчанию — имя файла профиля без расширения")
    parser.add_argument("--results-root", type=Path, default=Path("load-results"),
                        metavar="DIR", help="Куда писать результаты прогонов")
    parser.add_argument("--save-raw", action="store_true",
                        help="Сохранить сырые результаты запросов. Без них не будет графиков, "
                             "но на длинном прогоне это сотни мегабайт. ВНИМАНИЕ: в файл "
                             "прогона уходят тела запросов целиком и до 500 символов тела "
                             "ответа на каждую ошибку, без маскировки — там бывают учётные "
                             "и персональные данные")
    parser.add_argument("--dry-run", action="store_true",
                        help="Разобрать и показать конфигурацию, ничего не отправлять")
    parser.add_argument("--report", type=Path, metavar="FILE",
                        help="Собрать HTML-дашборд по каталогу результатов в указанный файл")
    parser.add_argument("--report-only", action="store_true",
                        help="Только собрать дашборд по уже сохранённым прогонам")
    parser.add_argument("--plotly-js", type=Path, metavar="FILE",
                        help="Файл plotly.js для вшивания в отчёт. Без него берётся из пакета "
                             "plotly; из сети не берётся никогда")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Подробность журнала прогона")
    parser.add_argument("--debug", action="store_true",
                        help="Показывать трассировку неожиданной ошибки")
    return parser


def _profile_name(args: argparse.Namespace) -> str:
    return args.profile_name or args.profile.stem


# Печатается при `--save-raw` до нагрузки, а не после: узнавать о том, что в файле лежат
# чужие данные, полезно до того, как он появился. Маскировки здесь нет намеренно — своей
# быть не должно (секреты редактирует `partest.redact`), а подключение `partest` пока не
# сделано: его импорт тянет за собой загрузку конфигурации набора тестов.
_SAVE_RAW_WARNING = (
    "ВНИМАНИЕ: --save-raw пишет в файл прогона тела запросов целиком, до 500 символов "
    "тела ответа на каждую ошибку и адрес каждого запроса с подставленными значениями "
    "path_params — без маскировки. Там бывают учётные данные и чужие персональные данные: "
    "файл прогона и собранный по нему дашборд не публикуйте и не передавайте как есть."
)


def _load_endpoints(path: Path) -> Dict[str, EndpointConfig]:
    """Описания эндпоинтов; предупреждения схемы — строками в stderr, а не трассой модуля.

    Предупреждения печатаются и тогда, когда разбор упал: ключ, который ни на что не
    влияет, бывает ровно тем, что человек пытался настроить.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConfigKeyWarning)
        try:
            return load_endpoints(path)
        finally:
            for item in caught:
                if issubclass(item.category, ConfigKeyWarning):
                    print(f"предупреждение: {path}: {item.message}", file=sys.stderr)


def _describe_plan(engine, with_notes: bool = True) -> list:
    """Строки плана для человека: фаза, потолок, темп, предел в полёте, список batch.

    Печатается до нагрузки и на `--dry-run`: сколько нагрузки даст профиль, видно до
    того, как она пошла на стенд. `with_notes` — печатать ли `plan_notes()`: на настоящем
    прогоне их пишет в журнал `run_load_test`, и второй раз они не нужны.
    """
    profile = engine.profile
    phases = engine.plan()
    total = sum(phase.duration_sec for phase in phases)
    hold = getattr(profile, "hold_after_breaking_sec", 0) or 0
    held = f" (+{hold:g} с удержания, если порог сработает)" if hold else ""
    lines = [f"план: фаз {len(phases)}, до {total:g} с{held}; воркеров {profile.workers}",
             f"  {'фаза':<24} {'потолок':>8} {'темп, попыток/с':>16} {'в полёте/воркер':>16}"]
    for phase in phases:
        tempo = profile.workers * engine._phase_rate(phase)
        lines.append(f"  {phase.name:<24} {phase.concurrency:>8} {tempo:>16.1f} "
                     f"{engine._phase_requests_per_worker(phase):>16}")
        if phase.requests is not None:
            lines.append(f"    список: {phase.requests} запросов "
                         "(варианты эндпоинта × repeat_count), фаза кончается на нём")
    if hold:
        lines.append(f"  {'hold-after-break':<24} уровень отказа, {hold:g} с в темпе того "
                     "уровня, на котором сработал порог")
    lines.append("  темп номинальный: разброс пейсинга делает его в среднем на ~10% ниже; "
                 "потолок — предел одновременных запросов, а не их число")
    for note in engine.plan_notes() if with_notes else []:
        lines.append(f"  внимание: {note}")
    return lines


def print_summary(metrics: AggregatedMetrics, out=None, events=None) -> None:
    """Сводка прогона для человека. Единственное место в пакете, где уместна печать.

    Поток берётся в момент вызова, а не в момент определения функции: `sys.stdout` по
    умолчанию в сигнатуре запоминает тот поток, что был при импорте, и перенаправить вывод
    вызывающему уже нечем. `events` — события прогона: остановка по порогу и итог
    списка batch печатаются отдельными строками.
    """
    out = out if out is not None else sys.stdout
    lines = [
        "",
        "=" * 60,
        "РЕЗУЛЬТАТЫ ПРОГОНА",
        "=" * 60,
        f"  запросов            : {metrics.total_requests:>8}",
        f"  RPS (реальный)      : {metrics.real_rps:>8.1f}",
        f"  p50 / p95 / p99     : {metrics.p50_ms:>5.0f} / {metrics.p95_ms:>5.0f} / {metrics.p99_ms:>5.0f} мс",
        f"  успешных            : {metrics.success_rate * 100:>7.1f}%",
        f"  ошибок              : {metrics.error_rate_pct:>7.1f}%",
    ]
    if metrics.errors_by_type:
        lines.append("  по типам ошибок:")
        for etype, count in sorted(metrics.errors_by_type.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {etype:20} : {count:>5}")
    if metrics.saturation_point_concurrency is not None:
        lines.append(f"  точка насыщения     : ≈ {metrics.saturation_point_concurrency}")
    lines.append(f"  деградация p95      : {metrics.degradation_slope_pct_per_min or 0.0:+.1f}% в минуту "
                 f"({metrics.degradation_status})")
    for event in events or []:
        if event.get("event") == "stopped":
            lines.append(f"  ОСТАНОВЛЕН по порогу: фаза {event.get('phase')}, "
                         f"{', '.join(event.get('reasons') or [])}")
        elif event.get("event") == "breached":
            lines.append(f"  ПОРОГ ДОСТИГНУТ (обрывать было нечего): фаза {event.get('phase')}, "
                         f"{', '.join(event.get('reasons') or [])}")
        elif event.get("event") == "batch_list":
            done = f"{event.get('completed')} из {event.get('planned')}"
            verdict = "пройден" if not event.get("reason") else f"НЕ пройден ({event['reason']})"
            lines.append(f"  список batch        : {done}, {verdict}")
    lines.append("=" * 60)
    lines.append("")
    print("\n".join(lines), file=out)


def _build_report(args: argparse.Namespace) -> int:
    """Сборка дашборда. Отчётные зависимости проверяются здесь, а не на импорте пакета."""
    try:
        from .reporting.generator import generate_dashboard
    except ImportError as exc:
        print(f"Отчёт собрать нечем: {exc}. Установите partest-load[report]", file=sys.stderr)
        return EXIT_ERROR

    target = args.report or (args.results_root / "dashboard.html")
    path = generate_dashboard(args.results_root, target, plotly_js=args.plotly_js)
    if path is None:
        print(f"В {args.results_root} нет сохранённых прогонов — дашборд не собран",
              file=sys.stderr)
        return EXIT_ERROR
    print(f"Дашборд собран: {path}")
    return EXIT_OK


def _run(endpoint_cfg: EndpointConfig, profile, args: argparse.Namespace) -> int:
    raw_results, metrics_obj, events = asyncio.run(run_load_test(endpoint_cfg, profile))
    aggregated = AggregatedMetrics.from_run_metrics(metrics_obj)
    print_summary(aggregated, events=events)

    # Снимок конфигурации уезжает в открытый JSON рядом с замерами и живёт там месяцами.
    # Красная линия 4 в AGENTS.md: секреты не попадают ни в отчёт, ни в артефакт. Что
    # выбросить, а что оставить, знает само описание эндпоинта — `snapshot()` оставляет
    # имена заголовков и метки вариантов тела, а значения не берёт.
    snapshot: Dict[str, Any] = {
        "endpoint": endpoint_cfg.snapshot(),
        "profile": profile.model_dump(mode="json"),
    }
    operation = endpoint_cfg.operation()
    saved = save_run_results(
        results_root=args.results_root,
        operation=operation,
        slug=args.endpoint,
        profile_name=_profile_name(args),
        metrics=aggregated,
        events=events,
        raw_results=raw_results if args.save_raw else None,
        config_snapshot=snapshot,
        save_raw=args.save_raw,
    )
    print(f"Результат сохранён: {saved.path}")
    latest = get_latest_run_path(args.results_root, operation, _profile_name(args))
    if latest is not None:
        print(f"Последний прогон этой операции: {latest}")
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx и httpcore на INFO пишут каждый запрос с полным адресом, включая значения
    # query_params (ключи API, токены); h2 и hpack на DEBUG — заголовки, в том числе
    # Authorization. Их журнал — не ниже WARNING при любом --log-level.
    for noisy in ("httpx", "httpcore", "h2", "hpack"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, getattr(logging, args.log_level)))

    if not args.report_only:
        missing = [name for name, value in
                   (("--endpoints", args.endpoints), ("--endpoint", args.endpoint),
                    ("--profile", args.profile)) if value is None]
        if missing:
            parser.error(f"для прогона нужны {', '.join(missing)}")

    try:
        if args.report_only:
            return _build_report(args)

        if args.save_raw:
            print(_SAVE_RAW_WARNING, file=sys.stderr)
        endpoints = _load_endpoints(args.endpoints)
        if args.endpoint not in endpoints:
            known = ", ".join(sorted(endpoints)) or "ни одного"
            print(f"В {args.endpoints} нет эндпоинта {args.endpoint!r}; есть: {known}",
                  file=sys.stderr)
            return EXIT_ERROR
        endpoint_cfg = endpoints[args.endpoint]
        profile = load_profile(args.profile)

        # Опознание прогона проверяется здесь, а не в `save_run_results`: там проверка
        # случалась уже **после** прогона, и негодное имя означало двадцать минут нагрузки,
        # затем отказ записи и потерянный результат. Операция входит в ту же проверку: она —
        # личность прогона, и выяснять её годность после нагрузки ещё дороже.
        validate_run_names(operation=endpoint_cfg.operation(), slug=args.endpoint,
                           profile_name=_profile_name(args))
        # Неподставленный `{placeholder}` пути — тоже отказ до нагрузки, и на сухом прогоне.
        endpoint_cfg.require_path_params()

        # Адрес первого варианта: у вариантов могут быть свои значения `{placeholders}`.
        first_url = _bare_base(endpoint_cfg.url(endpoint_cfg.payloads[0].path_params))
        print(f"эндпоинт {args.endpoint!r}: {endpoint_cfg.method} {first_url}")
        print(f"операция прогона: {endpoint_cfg.operation()}")
        print("профиль:")
        print(json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, indent=2))
        print("\n".join(_describe_plan(ENGINES[profile.type](profile, endpoint_cfg),
                                        with_notes=args.dry_run)))

        if args.dry_run:
            print("\nСухой прогон: ни один запрос не отправлен.")
            return EXIT_OK

        code = _run(endpoint_cfg, profile, args)
        if code == EXIT_OK and (args.report or args.plotly_js):
            code = _build_report(args)
        return code

    except KeyboardInterrupt:
        print("\nПрогон прерван с клавиатуры.", file=sys.stderr)
        return EXIT_INTERRUPTED
    # PhaseFailedError — RuntimeError, и это самый ожидаемый отказ прогона:
    # не дали сокет, нет h2, стенд не ответил. Он обязан быть строкой и кодом
    # возврата, а не стеком.
    except (ProfileError, ResultStoreError, OSError, ValueError, RuntimeError) as exc:
        if args.debug:
            raise
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
