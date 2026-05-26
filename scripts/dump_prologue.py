"""Dump the first 8 ARM64 instructions at a given NSO-relative offset.

Used to verify trampoline safety per CLAUDE.md gotcha #2: And64InlineHook
patches the first 5 instructions; any PC-relative op (adrp/adr/ldr-literal/
b/bl) in those bytes corrupts the trampoline.

Usage:
    python scripts/dump_prologue.py 0x124134 [0x935ce0 ...]
"""

import struct
import sys
import lz4.block
import capstone

NSO_PATH = r"C:\Users\maxwe\Desktop\Roms\Switch\Super Mario Bros. Wonder\main.nso"


def parse_nso(path):
    """Return decompressed .text and its load address (0)."""
    with open(path, "rb") as f:
        data = f.read()
    assert data[:4] == b"NSO0", "not an NSO file"
    flags = struct.unpack_from("<I", data, 0x0C)[0]
    # text segment header
    t_file_off, t_mem_off, t_dec_size = struct.unpack_from("<III", data, 0x10)
    t_comp_size = struct.unpack_from("<I", data, 0x60)[0]
    text_comp = data[t_file_off:t_file_off + t_comp_size]
    if flags & 1:
        text = lz4.block.decompress(text_comp, uncompressed_size=t_dec_size)
    else:
        text = text_comp
    return text, t_mem_off


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    targets = [int(a, 16) for a in sys.argv[1:]]
    text, mem_off = parse_nso(NSO_PATH)
    md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
    md.detail = True
    PC_REL = {"adrp", "adr", "ldr", "ldrsw", "b", "bl", "b.eq", "b.ne",
              "b.lt", "b.le", "b.gt", "b.ge", "b.hi", "b.lo", "b.cc",
              "b.cs", "b.al", "b.mi", "b.pl", "b.vs", "b.vc", "cbz",
              "cbnz", "tbz", "tbnz"}
    for off in targets:
        file_off = off - mem_off
        snippet = text[file_off:file_off + 32]
        print(f"\n=== NSO +0x{off:x} ===")
        prologue_unsafe = False
        for idx, insn in enumerate(md.disasm(snippet, off)):
            if idx >= 8:
                break
            mnem = insn.mnemonic.lower()
            # Mark only branches and PC-relative loads as unsafe.
            # `ldr xN, [reg, ...]` is fine; `ldr xN, label` (literal) is not.
            unsafe = False
            if mnem in {"adrp", "adr", "b", "bl"} or mnem.startswith("b."):
                unsafe = True
            if mnem in {"cbz", "cbnz", "tbz", "tbnz"}:
                unsafe = True
            if mnem in {"ldr", "ldrsw"} and "#0x" in insn.op_str.split(",")[-1] and "[" not in insn.op_str:
                # PC-relative ldr literal form: `ldr xN, #imm`
                unsafe = True
            flag = "  *** PC-REL ***" if unsafe and idx < 5 else ""
            if unsafe and idx < 5:
                prologue_unsafe = True
            byts = " ".join(f"{b:02x}" for b in insn.bytes)
            print(f"  +0x{insn.address:x}: {byts:<11} {mnem:<7} {insn.op_str}{flag}")
        if prologue_unsafe:
            print("  PROLOGUE TRAMPOLINE UNSAFE: PC-relative op in first 5 insns")
        else:
            print("  Prologue trampoline-safe: no PC-relative ops in first 5 insns")


if __name__ == "__main__":
    main()
