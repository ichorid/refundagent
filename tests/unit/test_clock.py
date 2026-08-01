from datetime import UTC, datetime

import pytest

from saferefund import clock


def teardown_function() -> None:
    clock.reset_now_for_tests()


def test_now_is_utc_aware() -> None:
    current_time = clock.now()

    assert current_time.tzinfo is UTC


def test_override_and_reset_restore_the_clock() -> None:
    fixed_time = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)

    clock.set_now_for_tests(fixed_time)

    assert clock.now() == fixed_time

    clock.reset_now_for_tests()

    assert clock.now() != fixed_time


def test_naive_override_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set_now_for_tests(datetime(2030, 1, 2, 3, 4))
