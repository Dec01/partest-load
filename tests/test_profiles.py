"""Разбор и проверка профиля нагрузки.

Формат YAML сохранён таким, каким он был в прототипе: ключи и значения по умолчанию здесь
закреплены, потому что профили потребителя написаны под них. Проверяется и второе: молчаливое
принятие негодного профиля — отдельный вид поломки, дороже падения.
"""

from __future__ import annotations

import pytest

from partest_load.configs.profiles import (
    BatchProfile,
    LinearProfile,
    ProfileError,
    RampProfile,
    StressProfile,
    load_profile,
    parse_profile,
)


def test_linear_defaults_match_the_prototype():
    profile = parse_profile({"type": "linear"})

    assert isinstance(profile, LinearProfile)
    assert (profile.duration_sec, profile.target_concurrency) == (300, 50)
    assert (profile.workers, profile.ramp_up_sec, profile.ramp_down_sec) == (6, 30, 0)
    assert profile.max_requests_per_worker == 30
    assert profile.rate_limit_rps == 10.0


def test_stress_defaults_match_the_prototype():
    profile = parse_profile({"type": "stress"})

    assert isinstance(profile, StressProfile)
    assert (profile.initial_concurrency, profile.max_concurrency) == (50, 600)
    assert (profile.ramp_up_step, profile.ramp_up_interval_sec) == (40, 20)
    assert profile.stop_on_error_rate == 8.0
    assert profile.stop_on_p99_latency_ms == 18000
    assert profile.stop_on_5xx_rate == 2.0
    assert (profile.max_duration_sec, profile.hold_after_breaking_sec) == (1200, 60)
    assert profile.workers == 10


def test_batch_and_ramp_defaults_match_the_prototype():
    batch = parse_profile({"type": "batch"})
    assert isinstance(batch, BatchProfile)
    assert (batch.duration_sec, batch.target_concurrency) == (600, 30)
    assert (batch.workers, batch.repeat_count) == (1, 3)

    ramp = parse_profile({"type": "ramp", "phases": [{"target_concurrency": 5}]})
    assert isinstance(ramp, RampProfile)
    assert ramp.workers == 8
    assert ramp.phases[0].duration_sec == 60


def test_rate_limit_delay_is_the_reciprocal_of_rps():
    """Движок получает паузу, а профиль задаёт частоту. Перепутать местами легко."""
    assert parse_profile({"type": "linear", "rate_limit_rps": 35}).rate_limit_delay == \
        pytest.approx(1 / 35)


def test_unknown_key_is_refused():
    """Опечатка в ключе не должна проходить молча.

    В прототипе профиль с `ramp_step` вместо `ramp_up_step` работал с чужими значениями по
    умолчанию и об этом не сообщал: кривая получалась не та, что написана в файле.
    """
    with pytest.raises(ProfileError, match="ramp_step"):
        parse_profile({"type": "stress", "ramp_step": 50})


def test_missing_type_names_the_known_types():
    with pytest.raises(ProfileError, match="linear"):
        parse_profile({"duration_sec": 10})


def test_unknown_type_is_refused():
    with pytest.raises(ProfileError):
        parse_profile({"type": "exponential"})


def test_ramp_phase_needs_both_ends_or_neither():
    """Фаза с одним концом — это не фаза с фиксированным уровнем, а ошибка в профиле."""
    with pytest.raises(ProfileError, match="вместе"):
        parse_profile({"type": "ramp", "phases": [
            {"duration_sec": 60, "start_concurrency": 40},
        ]})


def test_ramp_phase_needs_some_concurrency():
    with pytest.raises(ProfileError):
        parse_profile({"type": "ramp", "phases": [{"duration_sec": 60}]})


def test_ramp_without_phases_is_refused():
    with pytest.raises(ProfileError, match="phases"):
        parse_profile({"type": "ramp", "phases": []})


def test_stress_ladder_must_go_up():
    with pytest.raises(ProfileError, match="max_concurrency"):
        parse_profile({"type": "stress", "initial_concurrency": 500, "max_concurrency": 100})


def test_nonsense_numbers_are_refused():
    with pytest.raises(ProfileError):
        parse_profile({"type": "linear", "duration_sec": 0})
    with pytest.raises(ProfileError):
        parse_profile({"type": "linear", "rate_limit_rps": 0})
    with pytest.raises(ProfileError):
        parse_profile({"type": "linear", "target_concurrency": 0})
    with pytest.raises(ProfileError):
        parse_profile({"type": "linear", "stop_on_error_rate": 150})


def test_load_profile_reads_yaml_from_the_given_path(tmp_path):
    path = tmp_path / "steady.yaml"
    path.write_text(
        "name: Ровная нагрузка\n"
        "type: linear\n"
        "duration_sec: 120\n"
        "target_concurrency: 5\n"
        "workers: 3\n"
        "max_requests_per_worker: 1\n"
        "ramp_up_sec: 30\n"
        "ramp_down_sec: 20\n"
        "rate_limit_rps: 35\n"
        "stop_on_error_rate: 1.0\n",
        encoding="utf-8",
    )

    profile = load_profile(path)

    assert profile.name == "Ровная нагрузка"
    assert profile.target_concurrency == 5
    assert profile.max_requests_per_worker == 1
    assert profile.stop_on_error_rate == 1.0


def test_empty_and_broken_yaml_are_reported_as_profile_errors(tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ProfileError, match="пуст"):
        load_profile(empty)

    broken = tmp_path / "broken.yaml"
    broken.write_text("type: linear\n  bad indent: [\n", encoding="utf-8")
    with pytest.raises(ProfileError):
        load_profile(broken)
