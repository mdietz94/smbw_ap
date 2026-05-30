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


def _make_tree(root: Path, nso_body: str) -> Path:
    """Build a minimal switch-mod tree with sys/tools/nso.py."""
    tools = root / "sys" / "tools"
    tools.mkdir(parents=True)
    nso = tools / "nso.py"
    nso.write_text(nso_body, encoding="utf-8")
    return nso


def test_applies_on_clean_upstream(tmp_path: Path) -> None:
    """First-run case: upstream nso.py present, patch lands."""
    nso = _make_tree(tmp_path, _UPSTREAM_NSO_PY)
    results = P.apply_patches(tmp_path)
    assert [r.status for r in results] == ["applied"]
    patched = nso.read_text(encoding="utf-8")
    # Sentinel embedded so reruns short-circuit.
    assert P._NSO_SENTINEL in patched
    # Composition rewrite landed (no more `class X(struct.Struct)`).
    assert "class NsoSegment(struct.Struct)" not in patched
    assert "class NsoHeader(struct.Struct)" not in patched
    # The body AFTER the patched chunk is preserved verbatim.
    assert "self.magic = 0x304F534E" in patched


def test_already_applied_is_idempotent(tmp_path: Path) -> None:
    """Second-run case: sentinel present, file untouched."""
    nso = _make_tree(tmp_path, _UPSTREAM_NSO_PY)
    P.apply_patches(tmp_path)
    first = nso.read_text(encoding="utf-8")

    results = P.apply_patches(tmp_path)
    assert [r.status for r in results] == ["already-applied"]
    assert nso.read_text(encoding="utf-8") == first


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
