"""Tests for the LibHakkun in-tree patch step.

Covers the apply/skip/missing/upstream-shifted matrix plus a behavioural
check that the rewritten module actually instantiates on the current
interpreter (so a future tweak to ``_NSO_NEW`` can't silently break the
shape ``elf2nso.py`` expects).
"""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from apworld.smbw_archipelago._setup import patch_hakkun as P


# Verbatim copy of the upstream LibHakkun ``sys/tools/nso.py`` module body
# that the patch targets. Kept inline so the test stays standalone — touching
# the real submodule would couple the test to whatever's checked out.
# Mirrors the file at the pinned LibHakkun rev (9892726b).
_UPSTREAM_NSO_PY = '''\
import struct

class NsoSegment(struct.Struct):
    def __init__(self):
        super().__init__('<3I')

        self.file_offset = 0
        self.memory_offset = 0
        self.decompressed_size = 0

    def load(self, data, pos):
        (self.file_offset,
         self.memory_offset,
         self.decompressed_size) = self.unpack_from(data, pos)

    def save(self):
        return struct.pack(
            self.format,
            self.file_offset,
            self.memory_offset,
            self.decompressed_size,
        )


class NsoHeader(struct.Struct):
    def __init__(self):
        super().__init__('<4I12xI12xI12xI32s3I28s3Q32s32s32s')

        self.magic = 0x304F534E
        self.version = 0
        self._8 = 0
        self.flags = 0
        self.module_offset = 0
        self.module_file_size = 0
        self.bss_size = 0
        self.build_id = b'\\0' * 32
        self.text_compressed_size = 0
        self.rodata_compressed_size = 0
        self.data_compressed_size = 0
        self._6C = b'\\0' * 28
        self.api_info_extents = 0
        self.dynstr_extents = 0
        self.dynsym_extents = 0
        self.text_section_hash = b'\\0' * 32
        self.rodata_section_hash = b'\\0' * 32
        self.data_section_hash = b'\\0' * 32

        self.text_segment = NsoSegment()
        self.rodata_segment = NsoSegment()
        self.data_segment = NsoSegment()

    def load(self, data, pos=0):
        (self.magic,
         self.version,
         self._8,
         self.flags,
         self.module_offset,
         self.module_file_size,
         self.bss_size,
         self.build_id,
         self.text_compressed_size,
         self.rodata_compressed_size,
         self.data_compressed_size,
         self._6C,
         self.api_info_extents,
         self.dynstr_extents,
         self.dynsym_extents,
         self.text_section_hash,
         self.rodata_section_hash,
         self.data_section_hash) = self.unpack_from(data, pos)

        self.text_segment.load(data, pos + 0x10)
        self.rodata_segment.load(data, pos + 0x20)
        self.data_segment.load(data, pos + 0x30)

    def save(self):

        outBuffer = bytearray(struct.pack(
            self.format,
            self.magic,
            self.version,
            self._8,
            self.flags,
            self.module_offset,
            self.module_file_size,
            self.bss_size,
            self.build_id,
            self.text_compressed_size,
            self.rodata_compressed_size,
            self.data_compressed_size,
            self._6C,
            self.api_info_extents,
            self.dynstr_extents,
            self.dynsym_extents,
            self.text_section_hash,
            self.rodata_section_hash,
            self.data_section_hash,
        ))

        outBuffer[0x10:0x10 + self.text_segment.size] = self.text_segment.save()
        outBuffer[0x20:0x20 + self.rodata_segment.size] = self.rodata_segment.save()
        outBuffer[0x30:0x30 + self.data_segment.size] = self.data_segment.save()

        return outBuffer
'''


# Verbatim copies of the upstream LibHakkun cmake files the interpreter
# patch targets (pinned rev 9892726b). Trimmed to the lines that matter —
# each must contain at least one `COMMAND python ` so the rewrite has a
# target.
_UPSTREAM_DEPLOY_CMAKE = (
    "add_custom_command(TARGET ${PROJECT_NAME} POST_BUILD\n"
    "    COMMAND python ${CMAKE_CURRENT_SOURCE_DIR}/sys/tools/deploy.py "
    "${CMAKE_CURRENT_BINARY_DIR} ${PROJECT_NAME}\n"
    ")\n"
)
_UPSTREAM_GENEXEFS_CMAKE = (
    "function(generate_exefs)\n"
    "    add_custom_command(TARGET ${PROJECT_NAME} POST_BUILD\n"
    "        COMMAND python ${CMAKE_SOURCE_DIR}/sys/tools/senobi/build_npdm.py "
    "${CMAKE_CURRENT_BINARY_DIR}/npdm.json ${CMAKE_CURRENT_BINARY_DIR}/main.npdm\n"
    "        COMMAND python ${PROJECT_SOURCE_DIR}/sys/tools/elf2nso.py "
    "${CMAKE_CURRENT_BINARY_DIR}/${PROJECT_NAME} ${CMAKE_CURRENT_BINARY_DIR}/${PROJECT_NAME}.nso -c\n"
    "    )\n"
    "endfunction()\n"
)


# Verbatim copy of upstream's ``sys/tools/setup_libcxx_prepackaged.py``
# (pinned rev 9892726b) — the dead-GitHub-release fetch the URL patch
# targets. Reproduced with LF endings; the real file is CRLF, which
# ``read_text`` normalizes before the literal match runs.
_UPSTREAM_STDLIB_PY = '''\
#!/usr/bin/env python3

import os
import subprocess
import tarfile
import sys

is_aarch32 = len(sys.argv) > 1 and sys.argv[1] == 'aarch32'

prepackaged_source_tar_name = "stdlib-aarch32-19.1.0_clang_19.1.7.tar.xz" if is_aarch32 else "stdlib-19.1.0_clang_19.1.7.tar.xz"
prepackaged_source = "https://github.com/fruityloops1/LibHakkun/releases/download/stdlib-19.1.0-3/" + prepackaged_source_tar_name

root_dir = os.getcwd()

def downloadAndExtractPrepackaged():
    print(f"Downloading pre-packaged stdlib")

    subprocess.run(['curl', '-O', '-L', prepackaged_source])

    print(f"Extracting")

    src_tar = tarfile.open(prepackaged_source_tar_name)
    src_tar.extractall('.')
    src_tar.close()

    os.remove(prepackaged_source_tar_name)

downloadAndExtractPrepackaged()
'''


def _make_tree(
    root: Path,
    nso_body: str,
    *,
    with_cmake: bool = True,
    with_stdlib: bool = True,
) -> Path:
    """Build a minimal switch-mod tree with sys/tools/nso.py.

    When ``with_cmake`` (the default), also lays down the two cmake files
    the interpreter patch targets so the full patch set can apply.
    ``with_stdlib`` does the same for setup_libcxx_prepackaged.py.
    """
    tools = root / "sys" / "tools"
    tools.mkdir(parents=True)
    nso = tools / "nso.py"
    nso.write_text(nso_body, encoding="utf-8")
    if with_stdlib:
        (tools / "setup_libcxx_prepackaged.py").write_text(
            _UPSTREAM_STDLIB_PY, encoding="utf-8"
        )
    if with_cmake:
        cmake = root / "sys" / "cmake"
        cmake.mkdir(parents=True)
        (cmake / "deploy.cmake").write_text(_UPSTREAM_DEPLOY_CMAKE, encoding="utf-8")
        (cmake / "generate_exefs.cmake").write_text(
            _UPSTREAM_GENEXEFS_CMAKE, encoding="utf-8"
        )
    return nso


def test_applies_on_clean_upstream(tmp_path: Path) -> None:
    """First-run case: upstream nso.py + cmake present, all patches land."""
    nso = _make_tree(tmp_path, _UPSTREAM_NSO_PY)
    results = P.apply_patches(tmp_path)
    assert [r.status for r in results] == [
        "applied", "applied", "applied", "applied",
    ]
    patched = nso.read_text(encoding="utf-8")
    # Sentinel embedded so reruns short-circuit.
    assert P._NSO_SENTINEL in patched
    # Composition rewrite landed (no more `class X(struct.Struct)`).
    assert "class NsoSegment(struct.Struct)" not in patched
    assert "class NsoHeader(struct.Struct)" not in patched
    # The body AFTER the patched chunk is preserved verbatim.
    assert "self.magic = 0x304F534E" in patched


def test_already_applied_is_idempotent(tmp_path: Path) -> None:
    """Second-run case: every sentinel present, files untouched."""
    nso = _make_tree(tmp_path, _UPSTREAM_NSO_PY)
    P.apply_patches(tmp_path)
    first = nso.read_text(encoding="utf-8")
    deploy = tmp_path / "sys" / "cmake" / "deploy.cmake"
    first_deploy = deploy.read_text(encoding="utf-8")

    results = P.apply_patches(tmp_path)
    assert [r.status for r in results] == [
        "already-applied", "already-applied", "already-applied",
        "already-applied",
    ]
    assert nso.read_text(encoding="utf-8") == first
    assert deploy.read_text(encoding="utf-8") == first_deploy


def test_missing_file_reported(tmp_path: Path) -> None:
    """Submodule-not-initialized case: nso.py absent, reported but no raise."""
    results = P.apply_patches(tmp_path)
    assert results[0].status == "missing"
    assert "not found" in results[0].detail


def test_upstream_shifted_reported(tmp_path: Path) -> None:
    """Refactored-upstream case: nso.py exists but old text doesn't match."""
    _make_tree(tmp_path, "import struct\n# totally different upstream tree\n")
    results = P.apply_patches(tmp_path)
    assert results[0].status == "upstream-shifted"
    assert "needs to be refreshed" in results[0].detail


def test_on_line_callback_receives_status(tmp_path: Path) -> None:
    """on_line stream surfaces the patch result so cmake's progress
    log reflects what we did (or didn't)."""
    _make_tree(tmp_path, _UPSTREAM_NSO_PY)
    lines: list[str] = []
    P.apply_patches(tmp_path, on_line=lines.append)
    assert any("applied" in line and "nso.py" in line for line in lines)


def test_patched_module_instantiates(tmp_path: Path) -> None:
    """Behavioural: the rewritten nso.py must work end-to-end on the
    current interpreter. Catches accidental typos in the composition
    block that would only surface at cmake POST_BUILD time otherwise.

    We import the patched file and instantiate NsoHeader + NsoSegment.
    The upstream nso.py module is self-contained (no relative imports),
    so runpy is enough.
    """
    nso = _make_tree(tmp_path, _UPSTREAM_NSO_PY)
    P.apply_patches(tmp_path)

    ns = runpy.run_path(str(nso))
    NsoHeader = ns["NsoHeader"]
    NsoSegment = ns["NsoSegment"]

    seg = NsoSegment()
    assert seg.size == 12  # '<3I'
    assert seg.file_offset == 0

    hdr = NsoHeader()
    # Verifies the composition wiring: .format pulled from the inner
    # struct.Struct, .save() round-trips through struct.pack.
    payload = hdr.save()
    assert isinstance(payload, (bytes, bytearray))
    assert len(payload) == hdr._fmt.size


def test_cmake_interpreter_patch_replaces_python(tmp_path: Path) -> None:
    """deploy.cmake + generate_exefs.cmake get every bare `COMMAND python `
    routed through ${SMBWAP_PYTHON}, with no bare invocation left behind."""
    _make_tree(tmp_path, _UPSTREAM_NSO_PY)
    P.apply_patches(tmp_path)

    for name in ("deploy.cmake", "generate_exefs.cmake"):
        text = (tmp_path / "sys" / "cmake" / name).read_text(encoding="utf-8")
        assert P._CMAKE_PY_SENTINEL in text
        assert "COMMAND ${SMBWAP_PYTHON} " in text
        # No bare `COMMAND python ` survives (the failure mode we're fixing).
        assert "COMMAND python " not in text


def test_cmake_interpreter_patch_is_idempotent(tmp_path: Path) -> None:
    """Re-running leaves the cmake files byte-identical (sentinel guard)."""
    _make_tree(tmp_path, _UPSTREAM_NSO_PY)
    P.apply_patches(tmp_path)
    deploy = tmp_path / "sys" / "cmake" / "deploy.cmake"
    once = deploy.read_text(encoding="utf-8")
    # The single-replacement guard must not double-rewrite the token.
    assert "${${SMBWAP_PYTHON}}" not in once
    P.apply_patches(tmp_path)
    assert deploy.read_text(encoding="utf-8") == once


def test_cmake_interpreter_patch_missing_reported(tmp_path: Path) -> None:
    """No cmake files (only nso.py): the two cmake patches report missing."""
    _make_tree(tmp_path, _UPSTREAM_NSO_PY, with_cmake=False)
    results = P.apply_patches(tmp_path)
    statuses = [r.status for r in results]
    assert statuses == ["applied", "missing", "missing", "applied"]
    assert any("not found" in r.detail for r in results[1:3])


def test_cmake_interpreter_patch_upstream_shifted(tmp_path: Path) -> None:
    """A cmake file that no longer carries `COMMAND python ` is reported
    as upstream-shifted rather than silently mangled."""
    _make_tree(tmp_path, _UPSTREAM_NSO_PY)
    deploy = tmp_path / "sys" / "cmake" / "deploy.cmake"
    deploy.write_text(
        "add_custom_command(TARGET x POST_BUILD\n"
        "    COMMAND ${Python3_EXECUTABLE} sys/tools/deploy.py\n)\n",
        encoding="utf-8",
    )
    results = P.apply_patches(tmp_path)
    deploy_result = next(r for r in results if "deploy.cmake" in r.name)
    assert deploy_result.status == "upstream-shifted"
    assert "refreshed" in deploy_result.detail


def test_cmake_patch_token_matches_real_submodule_if_present() -> None:
    """Guardrail: if the LibHakkun submodule is checked out, the two
    target cmake files must still contain the bare `COMMAND python ` token
    our patch keys on — otherwise a release would ship a no-op patch and
    the lz4 build failure would return.

    Skipped when the submodule isn't initialized, or was already patched
    in-place by a prior build run.
    """
    here = Path(__file__).resolve()
    repo = here.parents[4]
    cmake_dir = repo / "switch-mod" / "sys" / "cmake"
    if not (cmake_dir / "deploy.cmake").is_file():
        pytest.skip("LibHakkun submodule not initialized")
    for rel in P._CMAKE_PY_TARGETS:
        target = repo / "switch-mod" / rel
        content = target.read_text(encoding="utf-8")
        if P._CMAKE_PY_SENTINEL in content:
            continue  # already patched in-place by a prior build run
        assert P._CMAKE_PY_OLD in content, (
            f"{target} no longer contains `{P._CMAKE_PY_OLD.strip()}`; "
            f"LibHakkun changed how it invokes Python and the interpreter "
            f"patch needs to be refreshed"
        )


def test_patch_old_string_matches_real_submodule_if_present() -> None:
    """Guardrail: if the actual LibHakkun submodule is checked out in
    this dev clone, our ``_NSO_OLD`` must still match what's on disk --
    otherwise we'd ship an apworld whose patch silently 'upstream-shifted'.

    Skipped on hosts without the submodule (CI without --recursive,
    packaged install). Only fires for dev-clone users who'd ship a
    release.
    """
    here = Path(__file__).resolve()
    repo = here.parents[4]  # tests → _setup → smbw_archipelago → apworld → repo
    real = repo / "switch-mod" / "sys" / "tools" / "nso.py"
    if not real.is_file():
        pytest.skip("LibHakkun submodule not initialized")
    content = real.read_text(encoding="utf-8")
    if P._NSO_SENTINEL in content:
        pytest.skip("submodule already patched in-place by a prior build run")
    assert P._NSO_OLD in content, (
        f"_NSO_OLD no longer matches {real}; LibHakkun upstream changed "
        f"nso.py and the patch needs to be refreshed"
    )


def test_stdlib_url_patch_repoints_download(tmp_path: Path) -> None:
    """setup_libcxx_prepackaged.py stops fetching from the dead GitHub
    release and starts fetching from Codeberg, with a failing curl."""
    _make_tree(tmp_path, _UPSTREAM_NSO_PY)
    P.apply_patches(tmp_path)

    text = (tmp_path / P._STDLIB_TARGET).read_text(encoding="utf-8")
    assert P._STDLIB_SENTINEL in text
    assert P._STDLIB_URL_NEW in text
    # The dead host must not survive anywhere in the file — that URL 404s
    # with a 9-byte body and is the whole bug.
    assert "github.com/fruityloops1/LibHakkun/releases" not in text
    # curl must now fail loudly rather than writing the 404 body to disk.
    assert "'--fail'" in text
    assert "raise SystemExit(" in text
    # Everything after the patched chunk is preserved verbatim.
    assert "src_tar = tarfile.open(prepackaged_source_tar_name)" in text
    assert "downloadAndExtractPrepackaged()" in text


def test_stdlib_url_patch_stays_valid_python(tmp_path: Path) -> None:
    """Behavioural: the rewritten script must still compile. The patched
    block is a chunk of hand-written source inside a string literal, so a
    stray quote would otherwise only surface at cmake configure time.

    ``compile`` rather than exec/runpy — running it would hit the network.
    """
    _make_tree(tmp_path, _UPSTREAM_NSO_PY)
    P.apply_patches(tmp_path)

    target = tmp_path / P._STDLIB_TARGET
    compile(target.read_text(encoding="utf-8"), str(target), "exec")


def test_stdlib_url_patch_is_idempotent(tmp_path: Path) -> None:
    """Re-running leaves the script byte-identical (sentinel guard)."""
    _make_tree(tmp_path, _UPSTREAM_NSO_PY)
    P.apply_patches(tmp_path)
    target = tmp_path / P._STDLIB_TARGET
    once = target.read_text(encoding="utf-8")
    P.apply_patches(tmp_path)
    assert target.read_text(encoding="utf-8") == once


def test_stdlib_url_patch_upstream_shifted(tmp_path: Path) -> None:
    """A script that no longer carries the expected fetch block is
    reported rather than silently mangled."""
    _make_tree(tmp_path, _UPSTREAM_NSO_PY)
    (tmp_path / P._STDLIB_TARGET).write_text(
        "# upstream rewrote this entirely\n", encoding="utf-8"
    )
    results = P.apply_patches(tmp_path)
    stdlib_result = next(r for r in results if "stdlib URL" in r.name)
    assert stdlib_result.status == "upstream-shifted"
    assert "needs to be refreshed" in stdlib_result.detail


def test_stdlib_patch_old_string_matches_real_submodule_if_present() -> None:
    """Guardrail: if the LibHakkun submodule is checked out, ``_STDLIB_OLD``
    must still match on disk — otherwise a release would ship a no-op patch
    and every fresh install would die at the compiler check again.

    Skipped when the submodule isn't initialized, or was already patched
    in-place by a prior build run.
    """
    here = Path(__file__).resolve()
    repo = here.parents[4]
    real = repo / "switch-mod" / P._STDLIB_TARGET
    if not real.is_file():
        pytest.skip("LibHakkun submodule not initialized")
    content = real.read_text(encoding="utf-8")
    if P._STDLIB_SENTINEL in content:
        pytest.skip("submodule already patched in-place by a prior build run")
    assert P._STDLIB_OLD in content, (
        f"_STDLIB_OLD no longer matches {real}; LibHakkun upstream changed "
        f"setup_libcxx_prepackaged.py and the patch needs to be refreshed"
    )
