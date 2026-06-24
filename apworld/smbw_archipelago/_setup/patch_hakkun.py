"""Apply local-tree patches to the pinned LibHakkun submodule.

Two patches are applied:

1. **nso.py composition** (described below): make ``sys/tools/nso.py``
   safe on Python 3.14's reworked ``struct.Struct``.

2. **cmake build-time interpreter** (``_patch_cmake_python``): rewrite the
   bare ``COMMAND python`` invocations in ``sys/cmake/deploy.cmake`` and
   ``sys/cmake/generate_exefs.cmake`` to ``COMMAND ${SMBWAP_PYTHON}``.
   Those custom commands run ``deploy.py`` (``import lz4.block``) and
   ``elf2nso.py`` (``import elftools``) at POST_BUILD. Bare ``python`` is
   resolved from PATH, so on a box with more than one Python it can land
   on an interpreter that lacks the ``--user``-installed lz4/pyelftools,
   and the build dies with ``ModuleNotFoundError`` immediately after
   linking. ``switch-mod/CMakeLists.txt`` defaults ``SMBWAP_PYTHON`` to
   ``python``; build.py passes ``-DSMBWAP_PYTHON=<resolved interpreter>``
   so the tools run under the exact Python the wizard installed the deps
   into. (The configure-time ``python3`` calls in toolchain.cmake are left
   alone — they only need *a* python3 and are covered by build.py's PATH
   prepend + python3 shim.)

The nso.py patch: rewrite ``sys/tools/nso.py`` from inheritance to
composition over ``struct.Struct``. Upstream's
``class NsoSegment(struct.Struct)`` / ``class NsoHeader(struct.Struct)``
relies on ``struct.Struct.__new__`` accepting a no-arg call, with the
subclass supplying ``format`` from inside ``__init__`` via
``super().__init__(format)``. Python 3.14 reimplemented ``struct.Struct``
with Argument Clinic, making ``format`` strictly required in
``__new__``'s signature -- so ``NsoHeader()`` now raises::

    TypeError: Struct() missing required argument 'format' (pos 1)

at ``__new__`` before ``__init__`` ever runs. The symptom for users is a
CMake POST_BUILD failure at link time, when ``sys/cmake/SwitchTools.cmake``
invokes ``python sys/tools/elf2nso.py`` and the import-then-instantiate
sequence crashes at line 64 (``header = NsoHeader()``).

The patch rewrites both classes to OWN a ``struct.Struct`` instead of
inheriting from one. Public interface (``.size``, ``.format``,
``.unpack_from``, ``.load``, ``.save``) and constructor shape are
preserved, so ``elf2nso.py`` works unchanged. Composition is durable
across every CPython version because it never relies on
``struct.Struct``'s constructor signature.

This script is invoked from :func:`build.cmake_configure` before the
cmake spawn so the patched ``nso.py`` is on disk by the time cmake
generates its link rules. Idempotent: a sentinel comment is dropped at
the head of the rewritten block, so re-runs short-circuit. Mirrors the
``patch_hakkun.py`` pattern in our sister project ``smo_archipelago``
(see ``smo_archipelago/scripts/patch_hakkun.py`` patch #9).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

# Sentinel embedded in the rewritten nso.py. Re-runs detect this and
# skip. Distinct from smo_archipelago's SMO_HAKKUN_PATCH_9 so the two
# projects don't accidentally claim to have applied each other's patches
# if a dev checks out both into adjacent worktrees.
_NSO_SENTINEL = "SMBWAP_HAKKUN_PATCH_NSO"

# Exact byte sequence to find in upstream's tools/nso.py. Bounded at
# the smallest unique prefix that reaches the NsoHeader __init__ line
# (where the inheritance pattern bottoms out). Everything AFTER the
# matched chunk -- the body of NsoHeader.__init__ assigning self.magic
# etc., the load/save methods -- is left in place.
_NSO_OLD = (
    "import struct\n"
    "\n"
    "class NsoSegment(struct.Struct):\n"
    "    def __init__(self):\n"
    "        super().__init__('<3I')\n"
    "\n"
    "        self.file_offset = 0\n"
    "        self.memory_offset = 0\n"
    "        self.decompressed_size = 0\n"
    "\n"
    "    def load(self, data, pos):\n"
    "        (self.file_offset,\n"
    "         self.memory_offset,\n"
    "         self.decompressed_size) = self.unpack_from(data, pos)\n"
    "\n"
    "    def save(self):\n"
    "        return struct.pack(\n"
    "            self.format,\n"
    "            self.file_offset,\n"
    "            self.memory_offset,\n"
    "            self.decompressed_size,\n"
    "        )\n"
    "\n"
    "\n"
    "class NsoHeader(struct.Struct):\n"
    "    def __init__(self):\n"
    "        super().__init__('<4I12xI12xI12xI32s3I28s3Q32s32s32s')\n"
)

# Composition rewrite. Lands the sentinel as a header comment so the
# next run detects 'already applied' even if the wider file is reformatted.
_NSO_NEW = (
    f"# {_NSO_SENTINEL}: composition over inheritance.\n"
    "#\n"
    "# Upstream wrote these classes as `class X(struct.Struct)` with\n"
    "# a no-arg constructor that called super().__init__(format). That\n"
    "# pattern relies on struct.Struct.__new__ accepting zero args.\n"
    "# Python 3.14 made `format` strictly required in Struct.__new__\n"
    "# (Argument Clinic rewrite), so the no-arg subclass instantiation\n"
    "# now raises `TypeError: Struct() missing required argument\n"
    "# 'format' (pos 1)` at __new__ before __init__ runs. Composition\n"
    "# preserves the public interface (.size, .format, .unpack_from,\n"
    "# .load, .save) without depending on Struct's constructor shape,\n"
    "# so it works on every CPython version (3.10 through 3.14+).\n"
    "import struct\n"
    "\n"
    "class NsoSegment:\n"
    "    _fmt = struct.Struct('<3I')\n"
    "    size = _fmt.size\n"
    "    format = _fmt.format\n"
    "\n"
    "    def __init__(self):\n"
    "        self.file_offset = 0\n"
    "        self.memory_offset = 0\n"
    "        self.decompressed_size = 0\n"
    "\n"
    "    def unpack_from(self, data, pos):\n"
    "        return self._fmt.unpack_from(data, pos)\n"
    "\n"
    "    def load(self, data, pos):\n"
    "        (self.file_offset,\n"
    "         self.memory_offset,\n"
    "         self.decompressed_size) = self.unpack_from(data, pos)\n"
    "\n"
    "    def save(self):\n"
    "        return struct.pack(\n"
    "            self.format,\n"
    "            self.file_offset,\n"
    "            self.memory_offset,\n"
    "            self.decompressed_size,\n"
    "        )\n"
    "\n"
    "\n"
    "class NsoHeader:\n"
    "    _fmt = struct.Struct('<4I12xI12xI12xI32s3I28s3Q32s32s32s')\n"
    "    size = _fmt.size\n"
    "    format = _fmt.format\n"
    "\n"
    "    def unpack_from(self, data, pos):\n"
    "        return self._fmt.unpack_from(data, pos)\n"
    "\n"
    "    def __init__(self):\n"
)


# --- cmake build-time interpreter patch -------------------------------------

# Sentinel comment dropped at the head of each rewritten cmake file so
# re-runs short-circuit.
_CMAKE_PY_SENTINEL = "SMBWAP_HAKKUN_PATCH_PYEXE"

# The cmake files whose POST_BUILD tool calls import lz4 / pyelftools.
# Both use only `COMMAND python ` (no `python3`), so a single substitution
# covers every occurrence. Relative to the switch-mod root cmake reads.
_CMAKE_PY_TARGETS = (
    Path("sys") / "cmake" / "deploy.cmake",          # deploy.py  -> import lz4.block
    Path("sys") / "cmake" / "generate_exefs.cmake",  # elf2nso.py -> import elftools
)

# Token rewritten in each target. The trailing space keeps us from matching
# a hypothetical `python3` (the char after `python` there is `3`, not space).
_CMAKE_PY_OLD = "COMMAND python "
_CMAKE_PY_NEW = "COMMAND ${SMBWAP_PYTHON} "

_CMAKE_PY_HEADER = (
    f"# {_CMAKE_PY_SENTINEL}: build-time Python tools run through "
    f"${{SMBWAP_PYTHON}} (set by build.py to the wizard-resolved\n"
    f"# interpreter) instead of bare `python`, so the --user-installed "
    f"lz4/pyelftools are always importable. See patch_hakkun.py.\n"
)


PatchStatus = Literal["applied", "already-applied", "missing", "upstream-shifted"]


@dataclass
class PatchResult:
    """One patch outcome.

    ``status`` is the categorical result; ``detail`` is a human-readable
    one-liner suitable for logging (e.g. the path that was missing, or
    a hint about what upstream-shifted means). Wired into the build
    on_line stream so users see patch activity inline with cmake output.
    """
    name: str
    status: PatchStatus
    detail: str = ""


def _patch_file(
    path: Path,
    old: str,
    new: str,
    sentinel: str,
) -> PatchStatus:
    """Apply a literal-string patch. Idempotent via ``sentinel`` check.

    ``upstream-shifted`` (warning, not error) means neither the expected
    ``old`` text nor the sentinel is present. Most commonly this happens
    if LibHakkun's nso.py is refactored upstream. We return it so the
    caller can keep going -- if the patch is still needed, cmake will
    surface the original TypeError at link time and we'll refresh the
    patch then.
    """
    if not path.exists():
        return "missing"
    content = path.read_text(encoding="utf-8")
    if sentinel in content:
        return "already-applied"
    if old not in content:
        return "upstream-shifted"
    path.write_text(content.replace(old, new, 1), encoding="utf-8", newline="\n")
    return "applied"


def _patch_cmake_python(path: Path) -> PatchStatus:
    """Rewrite every ``COMMAND python `` in a cmake file to ``${SMBWAP_PYTHON}``.

    Idempotent via :data:`_CMAKE_PY_SENTINEL`. ``upstream-shifted`` (a
    warning, not an error) means the file no longer contains the bare-
    ``python`` token — most likely LibHakkun started honoring a Python var
    itself, in which case this patch is moot.
    """
    if not path.exists():
        return "missing"
    content = path.read_text(encoding="utf-8")
    if _CMAKE_PY_SENTINEL in content:
        return "already-applied"
    if _CMAKE_PY_OLD not in content:
        return "upstream-shifted"
    patched = _CMAKE_PY_HEADER + content.replace(_CMAKE_PY_OLD, _CMAKE_PY_NEW)
    path.write_text(patched, encoding="utf-8", newline="\n")
    return "applied"


def apply_patches(
    switch_mod_root: Path,
    *,
    on_line: Callable[[str], None] | None = None,
) -> list[PatchResult]:
    """Apply all LibHakkun patches to the tree rooted at ``switch_mod_root``.

    ``switch_mod_root`` is the directory cmake reads as -S (i.e. contains
    ``CMakeLists.txt`` + ``sys/`` subdir). Returns one PatchResult per
    patch so the caller can log structured progress; raises only on
    programmer-error (not on patch-not-found, which is reported via
    PatchResult.status).
    """
    results: list[PatchResult] = []

    nso_py = switch_mod_root / "sys" / "tools" / "nso.py"
    status = _patch_file(nso_py, _NSO_OLD, _NSO_NEW, _NSO_SENTINEL)
    detail = ""
    if status == "missing":
        detail = f"{nso_py} not found (submodule not initialized?)"
    elif status == "upstream-shifted":
        detail = (
            f"{nso_py} doesn't match the expected upstream shape; "
            f"if cmake fails with `TypeError: Struct() missing required "
            f"argument 'format'`, the patch needs to be refreshed"
        )
    result = PatchResult(
        name="nso.py composition (Python 3.14 Struct.__new__ fix)",
        status=status,
        detail=detail,
    )
    results.append(result)
    if on_line is not None:
        msg = f"[patch_hakkun] {result.status:>17}  {result.name}"
        if result.detail:
            msg += f"  ({result.detail})"
        on_line(msg)

    # Patch 2: route build-time Python tools through ${SMBWAP_PYTHON}.
    for rel in _CMAKE_PY_TARGETS:
        cmake_path = switch_mod_root / rel
        status = _patch_cmake_python(cmake_path)
        detail = ""
        if status == "missing":
            detail = f"{cmake_path} not found (submodule not initialized?)"
        elif status == "upstream-shifted":
            detail = (
                f"{cmake_path} no longer contains `COMMAND python `; if the "
                f"build fails with `ModuleNotFoundError: No module named "
                f"'lz4'`, the patch needs to be refreshed"
            )
        result = PatchResult(
            name=f"cmake interpreter ({rel.name} -> $SMBWAP_PYTHON)",
            status=status,
            detail=detail,
        )
        results.append(result)
        if on_line is not None:
            msg = f"[patch_hakkun] {result.status:>17}  {result.name}"
            if result.detail:
                msg += f"  ({result.detail})"
            on_line(msg)

    return results
