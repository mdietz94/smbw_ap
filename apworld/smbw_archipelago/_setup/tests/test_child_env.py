"""Tests for the AppImage-aware child environment composer.

Motivating failure (real user report, Linux + Archipelago AppImage):
the wizard's cmake probe reported "not found on PATH" and the build
phase died with

    cmake: /tmp/.mount_archipMfEgoA/opt/Archipelago/lib/libssl.so.3:
        version `OPENSSL_3.2.0' not found (required by /usr/lib/libcurl.so.4)

because every child inherited the AppImage's LD_LIBRARY_PATH.
"""

from __future__ import annotations

import os

import pytest

from apworld.smbw_archipelago._setup import child_env as C

MOUNT = "/tmp/.mount_archipMfEgoA"


def _fake_appimage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(C.sys, "platform", "linux")
    monkeypatch.setenv("APPIMAGE", "/home/u/Archipelago.AppImage")
    monkeypatch.setenv("APPDIR", MOUNT)


def test_noop_when_not_under_appimage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.setattr(C.sys, "prefix", "/usr")
    monkeypatch.setattr(C.sys, "executable", "/usr/bin/python3")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/mylibs")
    assert C.running_under_appimage() is False
    assert C.child_environ("cmake")["LD_LIBRARY_PATH"] == "/opt/mylibs"


def test_strips_appimage_lib_path_for_host_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_appimage(monkeypatch)
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{MOUNT}/opt/Archipelago/lib")
    env = C.child_environ("/usr/bin/cmake")
    # Emptied entirely → the var must be gone, not blank (an empty
    # LD_LIBRARY_PATH is read as "current dir" by the loader).
    assert "LD_LIBRARY_PATH" not in env


def test_keeps_non_appimage_components(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_appimage(monkeypatch)
    monkeypatch.setenv(
        "LD_LIBRARY_PATH",
        os.pathsep.join([f"{MOUNT}/opt/Archipelago/lib", "/opt/cuda/lib64"]),
    )
    env = C.child_environ("/usr/bin/cmake")
    assert env["LD_LIBRARY_PATH"] == "/opt/cuda/lib64"


def test_strips_pythonhome_and_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host interpreter that inherits the bundle's PYTHONHOME can't
    even start — it looks for the AppImage's stdlib."""
    _fake_appimage(monkeypatch)
    monkeypatch.setenv("PYTHONHOME", f"{MOUNT}/opt/Archipelago")
    monkeypatch.setenv("PYTHONPATH", f"{MOUNT}/opt/Archipelago/lib/python3.12")
    env = C.child_environ("/usr/bin/python3")
    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env


def test_bundled_child_keeps_appimage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The AppImage's OWN binaries still need the bundle's loader paths,
    so a command that resolves inside the mount is left alone."""
    _fake_appimage(monkeypatch)
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{MOUNT}/opt/Archipelago/lib")
    env = C.child_environ(f"{MOUNT}/opt/Archipelago/bin/python3")
    assert env["LD_LIBRARY_PATH"] == f"{MOUNT}/opt/Archipelago/lib"


def test_mount_detected_without_appdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some AppRun scripts clear APPDIR; the mount dir is still
    discoverable from sys.prefix, and the `.mount_` segment alone is
    enough to recognize bundle paths."""
    monkeypatch.setattr(C.sys, "platform", "linux")
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(C.sys, "prefix", f"{MOUNT}/opt/Archipelago")
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{MOUNT}/opt/Archipelago/lib")
    assert C.running_under_appimage() is True
    assert "LD_LIBRARY_PATH" not in C.child_environ("/usr/bin/cmake")


def test_path_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """We deliberately do NOT filter PATH: the resolved-toolchain
    prepends in build.py own that, and dropping the bundle's bin dir
    could hide the interpreter the wizard itself is running under."""
    _fake_appimage(monkeypatch)
    monkeypatch.setenv("PATH", os.pathsep.join([f"{MOUNT}/usr/bin", "/usr/bin"]))
    assert C.child_environ("/usr/bin/cmake")["PATH"] == \
        os.pathsep.join([f"{MOUNT}/usr/bin", "/usr/bin"])


def test_shadowing_hint_matches_real_error() -> None:
    err = (
        f"cmake: {MOUNT}/opt/Archipelago/lib/libssl.so.3: version "
        f"`OPENSSL_3.2.0' not found (required by /usr/lib/libcurl.so.4)"
    )
    assert "AppImage" in C.appimage_shadowing_hint(err)


def test_shadowing_hint_ignores_unrelated_errors() -> None:
    assert C.appimage_shadowing_hint("cmake: command not found") == ""
    assert C.appimage_shadowing_hint("") == ""
    # A loader error that has nothing to do with an AppImage mount.
    assert C.appimage_shadowing_hint(
        "cmake: error while loading shared libraries: libfoo.so.1: "
        "cannot open shared object file"
    ) == ""
