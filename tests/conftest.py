"""Pytest configuration for the wpf-fork test suite.

Autouse fixtures here normalize the test environment so behavior matches
between local developer machines (where CI is typically unset) and the
GitHub Actions runner (which sets CI=true). Without this, tests that
exercise tools/ledger-event.py behave differently in CI: the script's
default --push behavior flips to True under CI=true and tries to
'git push origin' against the actual repository checkout, which 403s
because github-actions[bot] cannot push to protected branches.

Tests that specifically want CI=true semantics opt in via
``with patch.dict("os.environ", {"CI": "true"})`` inside the test body
(see test_ci_push_is_called, test_ci_gpg_failure_exits_nonzero, etc.).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _scrub_ci_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove CI=true from the environment for every test by default.

    Tests can re-enable CI semantics within their own body via
    ``patch.dict("os.environ", {"CI": "true"})``.
    """
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    yield
