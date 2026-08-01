"""Demo script is deterministic and exits cleanly."""

import subprocess
import sys

from saferefund.demo import DEMO_MESSAGE_ID, run_demo


def test_demo_exit_zero() -> None:
    """Demo completes with exit code zero."""
    assert run_demo() == 0


def test_demo_deterministic_output() -> None:
    """Repeated demo runs produce identical stdout."""
    first = subprocess.run(
        [sys.executable, "-m", "saferefund.demo"],
        capture_output=True,
        text=True,
        check=False,
    )
    second = subprocess.run(
        [sys.executable, "-m", "saferefund.demo"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0
    assert second.returncode == 0
    assert first.stdout == second.stdout
    assert DEMO_MESSAGE_ID in first.stdout or "case_1" in first.stdout
