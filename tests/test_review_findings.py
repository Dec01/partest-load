"""Сторожа для дефектов, найденных при ревью переноса.

Каждый тест здесь заведён вместе с починкой и проверен на том, что он падает на коде до
неё. Тест, написанный после починки и не проверенный на поломке, даёт ощущение покрытия
вместо покрытия — а все четыре дефекта ниже именно так и дожили до ревью.
"""

from __future__ import annotations

from partest_load.configs.endpoints import EndpointConfig
from partest_load.configs.profiles import LinearProfile
from partest_load.core.metrics import RunMetrics
from partest_load.engines.linear import FLOOR_CONCURRENCY, LinearEngine
from partest_load.storage.result_store import ResultStoreError, validate_run_names


def _endpoint(**extra) -> EndpointConfig:
    return EndpointConfig(name="probe", base_url="https://service.invalid",
                          path="/x", method="GET", **extra)


class TestRequestedLoadIsNotInflated:
    """Порог разгона не должен поднимать нагрузку выше заказанной."""

    def test_a_target_below_the_floor_is_not_raised_to_it(self):
        profile = LinearProfile(type="linear", duration_sec=60, target_concurrency=2,
                                workers=2, ramp_up_sec=10, ramp_down_sec=10)
        phases = LinearEngine(profile, _endpoint()).plan()

        assert phases, "план пуст"
        worst = max(p.concurrency for p in phases)
        assert worst <= profile.target_concurrency, (
            f"заказана параллельность {profile.target_concurrency}, "
            f"а план доходит до {worst}: "
            f"{[(p.name, p.concurrency) for p in phases]}"
        )

    def test_the_ramp_down_never_exceeds_the_plateau(self):
        profile = LinearProfile(type="linear", duration_sec=60, target_concurrency=2,
                                workers=2, ramp_up_sec=10, ramp_down_sec=10)
        phases = LinearEngine(profile, _endpoint()).plan()

        plateau = max(p.concurrency for p in phases)
        tail = [p for p in phases if "down" in p.name.lower() or "спад" in p.name.lower()]
        for phase in tail:
            assert phase.concurrency <= plateau, (
                f"спад {phase.name} идёт на {phase.concurrency} при плато {plateau}"
            )

    def test_a_target_above_the_floor_still_ramps_from_it(self):
        """Починка не должна отменить сам порог там, где он осмыслен."""
        profile = LinearProfile(type="linear", duration_sec=60, target_concurrency=50,
                                workers=6, ramp_up_sec=30, ramp_down_sec=0)
        phases = LinearEngine(profile, _endpoint()).plan()

        assert min(p.concurrency for p in phases) == FLOOR_CONCURRENCY


class TestTheRunnerHandsMetricsOrderedResults:
    """Сортировку делает раннер — значит и проверять надо его.

    Первая редакция этого теста сортировала вход сама и потому не сторожила ничего:
    убери сортировку из раннера — тест останется зелёным.
    """

    def test_results_come_back_ordered_by_time(self, monkeypatch):
        import asyncio

        from partest_load.core import runner as runner_mod

        # Фаза отдаёт результаты так, как их собирает настоящий прогон: воркер за
        # воркером, то есть не по времени.
        out_of_order = [
            {"success": True, "duration_sec": 0.1, "request_start_ts": 5.0},
            {"success": True, "duration_sec": 0.1, "request_start_ts": 1.0},
            {"success": True, "duration_sec": 0.1, "request_start_ts": 9.0},
            {"success": True, "duration_sec": 0.1, "request_start_ts": 3.0},
        ]

        class _OnePhaseEngine:
            def __init__(self, profile, endpoint_cfg):
                pass

            async def generate_phases(self):
                yield list(out_of_order), 1

        monkeypatch.setitem(runner_mod.ENGINES, "linear", _OnePhaseEngine)

        profile = LinearProfile(type="linear", duration_sec=1, target_concurrency=1,
                                workers=1, ramp_up_sec=0, ramp_down_sec=0)
        results, _metrics, _events = asyncio.run(
            runner_mod.run_load_test(_endpoint(), profile))

        stamps = [r["request_start_ts"] for r in results]
        assert stamps == sorted(stamps), (
            f"раннер отдал метрикам неупорядоченный список: {stamps}"
        )


class TestRunNamesAreCheckedBeforeTheRun:
    """Обещание докстроки хранилища должно быть вызываемым отдельно."""

    def test_a_bad_signature_is_refused(self):
        try:
            validate_run_names(operation="GET /v1/items", slug="Bad Name",
                               profile_name="ok")
        except ResultStoreError as exc:
            assert "подпись эндпоинта" in str(exc)
        else:
            raise AssertionError("имя с пробелом и заглавными принято")

    def test_a_bad_profile_name_is_refused(self):
        try:
            validate_run_names(operation="GET /v1/items", slug="ok",
                               profile_name="Steady Profile")
        except ResultStoreError as exc:
            assert "имя профиля" in str(exc)
        else:
            raise AssertionError("имя профиля с пробелом принято")

    def test_a_bad_operation_is_refused(self):
        try:
            validate_run_names(operation="orders-create", slug="orders-create",
                               profile_name="steady")
        except ResultStoreError as exc:
            assert "операция прогона" in str(exc)
        else:
            raise AssertionError("слаг принят за операцию")

    def test_good_names_pass(self):
        validate_run_names(operation="GET /v1/items/{id}", slug="orders-create",
                           profile_name="ramp_search")


class TestSecretsDoNotReachTheArtifact:
    """Красная линия 4: секреты не попадают ни в отчёт, ни в артефакт."""

    def test_header_values_are_dropped_from_the_snapshot(self, monkeypatch, tmp_path):
        from partest_load import cli

        # Маркер намеренно не похож на учётные данные: тест проверяет, что из
        # снимка уходит **значение** заголовка, каким бы оно ни было, а строка
        # в форме настоящего токена ловится стражем публикации как инцидент.
        marker = "sentinel-authorization-value"
        endpoint = _endpoint(headers={"Authorization": marker, "X-Api-Key": "k" * 20})
        profile = LinearProfile(type="linear", duration_sec=1, target_concurrency=1,
                                workers=1, ramp_up_sec=0, ramp_down_sec=0)

        captured = {}

        class _Saved:
            path = "run.json"

        def fake_save(**kwargs):
            captured.update(kwargs)
            return _Saved()

        monkeypatch.setattr(cli, "save_run_results", fake_save)
        monkeypatch.setattr(cli, "run_load_test",
                            lambda *a, **k: _completed(([], RunMetrics(), [])))
        monkeypatch.setattr(cli, "print_summary", lambda *a, **k: None)

        args = cli.build_parser().parse_args([
            "--endpoints", "e.yaml", "--endpoint", "orders", "--profile", str(tmp_path / "p.yaml"),
            "--results-root", str(tmp_path),
        ])
        cli._run(endpoint, profile, args)

        dumped = repr(captured.get("config_snapshot"))
        assert marker not in dumped, "значение Authorization уехало в артефакт"
        assert "k" * 20 not in dumped, "значение X-Api-Key уехало в артефакт"
        assert "Authorization" in dumped, (
            "имена заголовков нужны: без них через месяц не понять, с какой авторизацией "
            "шёл прогон"
        )

    def test_payload_bodies_are_dropped_from_the_snapshot(self, monkeypatch, tmp_path):
        """Тело запроса в снимок не уезжает — ни одним полем.

        `EndpointConfig.snapshot()` написан именно для этого, но `cli` собирал снимок сам и
        брал `model_dump()`: логин с паролем из профиля, создающего данные, ложился в
        открытый JSON рядом с замерами и жил там месяцами.
        """
        from partest_load import cli

        marker = "sentinel-password-value"
        endpoint = EndpointConfig(name="probe", base_url="https://service.invalid",
                                  path="/v1/sessions", method="POST",
                                  payloads=[{"body": {"login": "u", "password": marker}}])
        profile = LinearProfile(type="linear", duration_sec=1, target_concurrency=1,
                                workers=1, ramp_up_sec=0, ramp_down_sec=0)

        captured = {}

        class _Saved:
            path = "run.json"

        def fake_save(**kwargs):
            captured.update(kwargs)
            return _Saved()

        monkeypatch.setattr(cli, "save_run_results", fake_save)
        monkeypatch.setattr(cli, "get_latest_run_path", lambda *a, **k: None)
        monkeypatch.setattr(cli, "run_load_test",
                            lambda *a, **k: _completed(([], RunMetrics(), [])))
        monkeypatch.setattr(cli, "print_summary", lambda *a, **k: None)

        args = cli.build_parser().parse_args([
            "--endpoints", "e.yaml", "--endpoint", "sessions",
            "--profile", str(tmp_path / "p.yaml"), "--results-root", str(tmp_path),
        ])
        cli._run(endpoint, profile, args)

        dumped = repr(captured.get("config_snapshot"))
        assert marker not in dumped, "значение из тела запроса уехало в артефакт"
        assert "password" in dumped, (
            "имена полей нужны: по метке варианта читатель отчёта понимает, чем шёл прогон"
        )


class TestTheRunIsIdentifiedByItsOperation:
    """Личность прогона — операция; подпись и ключ в файле эндпоинтов ею не являются."""

    def test_the_operation_comes_from_the_endpoint_description(self):
        """Операция выводится из метода и пути — значит разойтись с прогоном не может."""
        assert _endpoint().operation() == "GET /x"
        by_id = EndpointConfig(name="p", base_url="https://service.invalid",
                               path="v1/items/{id}", method="GET")
        assert by_id.operation() == "GET /v1/items/{id}"
        # Эндпоинт без пути — тоже операция, а не пустая строка.
        assert EndpointConfig(name="p", base_url="https://service.invalid",
                              method="GET").operation() == "GET /"

    def test_the_base_url_is_not_part_of_the_operation(self):
        """Один узел на тесте и на препроде — один узел.

        Войди база в операцию, и прогоны одного эндпоинта на двух стендах разъехались бы по
        разным каталогам, а сведение с картой потребовало бы знать все адреса.
        """
        test = EndpointConfig(name="p", base_url="https://test.invalid", path="/v1/items",
                              method="GET")
        preprod = EndpointConfig(name="p", base_url="https://preprod.invalid/api",
                                 path="/v1/items", method="GET")

        assert test.operation() == preprod.operation() == "GET /v1/items"

    def test_the_cli_checks_the_operation_before_any_load(self, monkeypatch, tmp_path, capsys):
        """Негодная операция — отказ до нагрузки, а не после.

        Проверка стоит рядом с разбором конфигурации: двадцать минут нагрузки и отказ записи
        — то, от чего эту проверку и вынесли. Если вызов уедет за `run_load_test`, тест
        упадёт на часовом, который считает, что нагрузка уже пошла.
        """
        from partest_load import cli

        def _must_not_run(*a, **k):
            raise AssertionError("проверка операции случилась после начала нагрузки")

        monkeypatch.setattr(cli, "run_load_test", _must_not_run)
        # Путь с пробелом операцией быть не может, а описание эндпоинта его принимает.
        endpoints = tmp_path / "endpoints.yaml"
        endpoints.write_text(
            "items:\n  name: x\n  base_url: https://service.invalid\n"
            "  path: /v1/bad path\n  method: GET\n", encoding="utf-8")
        profile = tmp_path / "steady.yaml"
        profile.write_text("name: x\ntype: linear\nduration_sec: 60\n"
                           "target_concurrency: 2\nworkers: 1\nramp_up_sec: 0\n"
                           "ramp_down_sec: 0\n", encoding="utf-8")

        argv = ["--endpoints", str(endpoints), "--endpoint", "items",
                "--profile", str(profile), "--results-root", str(tmp_path / "out")]

        assert cli.main(argv) == 1
        assert "операция прогона" in capsys.readouterr().err

        # И на сухом прогоне тоже: именно им проверяют конфигурацию перед настоящим
        # запуском, и «всё в порядке» здесь при отказе записи потом — худший ответ.
        assert cli.main(argv + ["--dry-run"]) == 1
        assert "операция прогона" in capsys.readouterr().err


def _completed(value):
    """Готовая корутина: `cli._run` оборачивает вызов в `asyncio.run`."""
    async def _coro(*a, **k):
        return value
    return _coro()
