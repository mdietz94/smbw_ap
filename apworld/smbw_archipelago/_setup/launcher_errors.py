"""Make swallowed launch-time errors visible to the user.

When the Archipelago Launcher is opened via a Windows file association
(double-click a `.smbwap`, or right-click → Open With → ArchipelagoLauncher),
its host process is the pythonw-style ArchipelagoLauncher.exe with no
console attached. Any traceback printed by us — or by a subprocess we
spawned via `launch_subprocess` — goes to a void, so an import-time
exception in `_setup.wizard` or a missing `.smbwap` file just looks like
"nothing happened" to the user.

This module exports a `visible_errors(context)` decorator that wraps the
apworld's subprocess entry points so any escape:
  1. lands a full traceback at `%APPDATA%/SMBWArchipelago/launch-crash.log`
  2. surfaces a Tk messagebox with the tail of the traceback
  3. is re-raised so test harnesses + exit codes still see the failure.

Lives in `_setup/` (not the apworld root) so unit tests can import it
without bouncing off `import Utils` (Archipelago-specific) in the world
package's __init__.py.
"""

from __future__ import annotations

import functools
import traceback
from typing import Any, Callable, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])


def show_launch_error(
    context: str,
    exc: BaseException,
    *,
    notifier: Callable[[str, str], None] | None = None,
    log_writer: Callable[[str], str | None] | None = None,
) -> None:
    """Surface a launch-time crash via log file + Tk messagebox.

    `notifier` and `log_writer` are injectable so tests can observe what
    would have been shown without actually popping windows or touching the
    real %APPDATA% directory. Defaults wire up Tk + `appdata_root()`.
    """
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    log_writer = log_writer if log_writer is not None else _default_log_writer
    log_path_msg = ""
    try:
        written_to = log_writer(f"=== SMBW Archipelago launch crash ({context}) ===\n{tb_text}")
        if written_to:
            log_path_msg = f"\n\nFull traceback written to:\n{written_to}"
    except Exception:
        pass

    notifier = notifier if notifier is not None else _default_notifier
    snippet = tb_text if len(tb_text) <= 1000 else "...\n" + tb_text[-1000:]
    try:
        notifier(
            f"SMBW Archipelago — {context} failed",
            f"{context} could not start.\n\n{snippet}{log_path_msg}",
        )
    except Exception:
        pass


def visible_errors(context: str) -> Callable[[_F], _F]:
    """Decorator: surface uncaught exceptions via `show_launch_error`, then
    re-raise so test harnesses + exit codes still observe the failure."""
    def deco(fn: _F) -> _F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except BaseException as e:
                show_launch_error(context, e)
                raise
        return wrapper  # type: ignore[return-value]
    return deco


def show_launch_warning(
    context: str,
    exc: BaseException,
    *,
    notifier: Callable[[str, str], None] | None = None,
    log_writer: Callable[[str], str | None] | None = None,
) -> None:
    """Surface a non-fatal launch-time problem via log file + Tk popup.

    Sibling of `show_launch_error` for the case where the caller has
    already decided launch can continue with degraded behavior (e.g.
    `.smbwap` parse failed → open SMBW Client with no pre-fill). Writes
    to a separate log file (`launch-warning.log`) so a subsequent fatal
    error doesn't overwrite this diagnostic, and uses `showwarning`
    (yellow triangle) rather than `showerror` to match the "launch is
    continuing" framing. Does NOT re-raise — the caller is recovering.
    """
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    log_writer = log_writer if log_writer is not None else _default_warning_log_writer
    log_path_msg = ""
    try:
        written_to = log_writer(f"=== SMBW Archipelago launch warning ({context}) ===\n{tb_text}")
        if written_to:
            log_path_msg = f"\n\nFull traceback written to:\n{written_to}"
    except Exception:
        pass

    notifier = notifier if notifier is not None else _default_warning_notifier
    snippet = tb_text if len(tb_text) <= 1000 else "...\n" + tb_text[-1000:]
    try:
        notifier(
            f"SMBW Archipelago — {context}",
            f"{context}\n\n{snippet}{log_path_msg}",
        )
    except Exception:
        pass


def _default_log_writer(text: str) -> str | None:
    """Write the crash log under %APPDATA%/SMBWArchipelago/. Returns the
    written-to path so the caller can include it in the messagebox."""
    from . import appdata_root  # local: this file is in the same package
    log_path = appdata_root() / "launch-crash.log"
    log_path.write_text(text, encoding="utf-8")
    return str(log_path)


def _default_warning_log_writer(text: str) -> str | None:
    """Like `_default_log_writer` but writes to `launch-warning.log` so a
    later fatal crash in the same launch session doesn't clobber the
    diagnostic for a recoverable problem (e.g. `.smbwap` parse
    failure)."""
    from . import appdata_root
    log_path = appdata_root() / "launch-warning.log"
    log_path.write_text(text, encoding="utf-8")
    return str(log_path)


def _default_notifier(title: str, body: str) -> None:
    """Pop a Tk messagebox. Tk is stdlib so should always be importable in
    environments where Python runs AP at all."""
    import tkinter
    import tkinter.messagebox
    root = tkinter.Tk()
    root.withdraw()
    try:
        tkinter.messagebox.showerror(title, body)
    finally:
        root.destroy()


def _default_warning_notifier(title: str, body: str) -> None:
    """Same as `_default_notifier` but uses `showwarning` (yellow triangle)
    to signal a non-fatal problem with continued execution."""
    import tkinter
    import tkinter.messagebox
    root = tkinter.Tk()
    root.withdraw()
    try:
        tkinter.messagebox.showwarning(title, body)
    finally:
        root.destroy()
