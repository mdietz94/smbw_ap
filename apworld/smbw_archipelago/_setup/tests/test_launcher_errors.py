"""Tests for the launcher_errors visible_errors decorator."""

from __future__ import annotations

from typing import Any

import pytest

from apworld.smbw_archipelago._setup.launcher_errors import (
    show_launch_error, show_launch_warning, visible_errors,
)


def test_visible_errors_reraises_and_notifies() -> None:
    notifications: list[tuple[str, str]] = []
    logs: list[str] = []

    @visible_errors("test context")
    def boom() -> None:
        raise RuntimeError("nope")

    # Patch the module-level defaults via monkey-injection of show_launch_error.
    import apworld.smbw_archipelago._setup.launcher_errors as le
    original = le.show_launch_error

    def fake_show(context: str, exc: BaseException, **kw: Any) -> None:
        notifications.append((context, type(exc).__name__))
        logs.append(str(exc))

    le.show_launch_error = fake_show  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="nope"):
            boom()
    finally:
        le.show_launch_error = original  # type: ignore[assignment]

    assert notifications == [("test context", "RuntimeError")]
    assert "nope" in logs[0]


def test_visible_errors_passes_through_success() -> None:
    @visible_errors("test")
    def ok() -> int:
        return 42

    assert ok() == 42


def test_show_launch_error_calls_notifier_and_logger() -> None:
    notifications: list[tuple[str, str]] = []
    logged: list[str] = []

    def notifier(title: str, body: str) -> None:
        notifications.append((title, body))

    def log_writer(text: str) -> str | None:
        logged.append(text)
        return "/fake/path/launch-crash.log"

    show_launch_error(
        "ctx",
        RuntimeError("boom"),
        notifier=notifier,
        log_writer=log_writer,
    )
    assert len(notifications) == 1
    title, body = notifications[0]
    assert "ctx" in title
    assert "boom" in body
    assert "/fake/path/launch-crash.log" in body
    assert logged and "boom" in logged[0]


def test_show_launch_error_truncates_long_traceback() -> None:
    """The messagebox should show only the tail of a huge traceback so
    the dialog stays usable. The log file gets the full text."""
    notifications: list[tuple[str, str]] = []
    logged: list[str] = []

    def notifier(title: str, body: str) -> None:
        notifications.append((title, body))

    def log_writer(text: str) -> str | None:
        logged.append(text)
        return None

    huge = RuntimeError("X" * 5000)
    show_launch_error("ctx", huge, notifier=notifier, log_writer=log_writer)
    body = notifications[0][1]
    # Body should contain the truncation marker for long stacks.
    assert len(body) < 2000
    # Log got the full content.
    assert "X" * 5000 in logged[0]


def test_show_launch_warning_does_not_raise() -> None:
    """Unlike show_launch_error, the warning helper never re-raises —
    the caller has already decided to keep going."""
    notifications: list[tuple[str, str]] = []
    show_launch_warning(
        "ctx",
        RuntimeError("recoverable"),
        notifier=lambda t, b: notifications.append((t, b)),
        log_writer=lambda _t: None,
    )
    assert len(notifications) == 1
    assert "recoverable" in notifications[0][1]


def test_show_launch_error_swallows_notifier_failure() -> None:
    """If the notifier itself raises (e.g. no display), the error
    handler must not propagate — we're already in error-handling code
    and a second failure would be worse than silence."""
    def bad_notifier(title: str, body: str) -> None:
        raise RuntimeError("no display")

    # Must not raise.
    show_launch_error(
        "ctx",
        RuntimeError("orig"),
        notifier=bad_notifier,
        log_writer=lambda _t: None,
    )
