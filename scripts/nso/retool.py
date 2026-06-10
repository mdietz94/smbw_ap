"""Minimal static-RE toolkit over the uncompressed SMBW v1.0.0 NSO.

No Ghidra needed: parses the NSO container directly (segments + MOD0 ->
.dynamic -> RELA, so vtables/data pointers read with their load-time values),
disassembles via capstone, and offers encoding-level scans (BL/B callers,
adrp+add string xrefs, exact-word finds) that stay fast over the 56 MB
binary.  This is what cracked the power-up negation choke point in one
session (static-analysis-findings.md "2026-06-10").

Input: an NSO with UNCOMPRESSED segments (flags & 7 == 0), e.g. produced by
hactoolnet --uncompressed.  Default path below; override with the
SMBW_NSO_DEC env var.  Requires `pip install capstone`.

VA convention: NSO-relative (same numbers as the smbw-re-map ledger, i.e.
Ghidra 0x7100000000+va). file = seg.fileoff + (va - seg.memoff).

Usage: `from retool import *` in a REPL/script, then dis()/callers()/
find_cstr()/adrp_add_refs()/vtable_dump()/grep_strings() etc.
"""
import os
import struct
import capstone

_NSO_PATH = os.environ.get(
    'SMBW_NSO_DEC', r'C:\Users\maxwe\Documents\smbw_re_tmp\main_dec.bin')
DATA = bytearray(open(_NSO_PATH, 'rb').read())
GHIDRA_BASE = 0x7100000000

def _seg(off):
    fo, mo, sz = struct.unpack_from('<III', DATA, off)
    return fo, mo, sz
TEXT = _seg(0x10)
RO = _seg(0x20)
DSEG = _seg(0x30)

def va2file(va):
    for fo, mo, sz in (TEXT, RO, DSEG):
        if mo <= va < mo + sz:
            return fo + (va - mo)
    raise ValueError('unmapped va %#x' % va)

def read(va, n):
    f = va2file(va)
    return DATA[f:f+n]

def u32(va):
    return struct.unpack('<I', read(va, 4))[0]

def u64(va):
    return struct.unpack('<Q', read(va, 8))[0]

def _apply_relocs():
    """Apply R_AARCH64_RELATIVE relocations so vtables/data pointers read as
    GHIDRA_BASE+addend (load-time view). MOD0 offset is at text VA +0x4."""
    mod0_off = struct.unpack_from('<I', DATA, va2file(0x4))[0]
    mod0 = mod0_off  # VA of MOD0 header
    magic = read(mod0, 4)
    assert magic == b'MOD0', magic
    dyn_off = struct.unpack_from('<i', DATA, va2file(mod0 + 4))[0]
    dyn = mod0 + dyn_off
    rela = relasz = None
    v = dyn
    while True:
        tag, val = struct.unpack_from('<qQ', DATA, va2file(v))
        if tag == 0:
            break
        if tag == 7:
            rela = val
        elif tag == 8:
            relasz = val
        v += 16
    n = 0
    if rela is not None:
        f = va2file(rela)
        for e in range(relasz // 24):
            off, info, addend = struct.unpack_from('<QQq', DATA, f + e*24)
            if info == 0x403:  # R_AARCH64_RELATIVE
                struct.pack_into('<Q', DATA, va2file(off), GHIDRA_BASE + addend)
                n += 1
    return n

MD = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN)
MD.detail = False

def dis(va, n=16, out=True):
    insns = list(MD.disasm(read(va, 4*n), GHIDRA_BASE + va))
    if out:
        for i in insns:
            print('  %#x: %s %s' % (i.address - GHIDRA_BASE, i.mnemonic, i.op_str))
    return insns

def insn_at(va):
    l = list(MD.disasm(read(va, 4), GHIDRA_BASE + va))
    return l[0] if l else None

# ---- raw word scans over .text ----
_text_words = None
def text_words():
    global _text_words
    if _text_words is None:
        fo, mo, sz = TEXT
        _text_words = struct.unpack_from('<%dI' % (sz // 4), DATA, fo)
    return _text_words

def find_word(enc):
    """All text VAs holding exactly this u32."""
    w = text_words()
    return [i*4 for i, v in enumerate(w) if v == enc]

def callers(target_va):
    """All BL sites in .text that call target_va."""
    w = text_words()
    hits = []
    for i, v in enumerate(w):
        if (v >> 26) == 0b100101:  # BL
            imm = v & 0x3FFFFFF
            if imm & 0x2000000:
                imm -= 0x4000000
            if i*4 + imm*4 == target_va:
                hits.append(i*4)
    return hits

def b_callers(target_va):
    """All B (unconditional branch, tail-call) sites targeting target_va."""
    w = text_words()
    hits = []
    for i, v in enumerate(w):
        if (v >> 26) == 0b000101:  # B
            imm = v & 0x3FFFFFF
            if imm & 0x2000000:
                imm -= 0x4000000
            if i*4 + imm*4 == target_va:
                hits.append(i*4)
    return hits

def func_start(va, max_back=0x4000):
    """Walk back to a likely function start: the insn after a RET/B-only
    boundary, or a pacibsp/`stp x29,x30` style prologue. Heuristic."""
    v = va & ~3
    best = None
    while v > max(0, va - max_back):
        word = u32(v)
        # pacibsp = 0xD503237F
        if word == 0xD503237F:
            return v
        # sub sp, sp, #imm  : 0xD10003FF base
        if (word & 0xFF8003FF) == 0xD10003FF:
            best = v
        # stp x29, x30, [sp, #-imm]! : 0xA9807BFD masked
        if (word & 0xFFC07FFF) == 0xA9807BFD:
            best = v
        prev = u32(v - 4)
        # previous is RET (0xD65F03C0) or B (top6=000101) => v is a start
        if prev == 0xD65F03C0 or (prev >> 26) == 0b000101 or prev == 0xD503201F:
            # require current insn looks like a prologue-ish or any code
            return v
        v -= 4
    return best or v

# ---- strings ----
def find_cstr(s, all_hits=False):
    """Find a NUL-terminated string in .rodata (or anywhere); returns VAs."""
    pat = s.encode() + b'\x00'
    hits = []
    start = 0
    while True:
        f = DATA.find(pat, start)
        if f < 0:
            break
        # map file offset back to VA
        for fo, mo, sz in (TEXT, RO, DSEG):
            if fo <= f < fo + sz:
                hits.append(mo + (f - fo))
                break
        start = f + 1
        if not all_hits and hits:
            break
    return hits

def grep_strings(substr, limit=200):
    """All NUL-terminated printable strings in .rodata containing substr."""
    fo, mo, sz = RO
    blob = DATA[fo:fo+sz]
    out = []
    start = 0
    needle = substr.encode()
    while len(out) < limit:
        f = blob.find(needle, start)
        if f < 0:
            break
        s = blob.rfind(b'\x00', 0, f) + 1
        e = blob.find(b'\x00', f)
        if e < 0:
            break
        bs = blob[s:e]
        try:
            txt = bs.decode('ascii')
            if all(32 <= ord(c) < 127 for c in txt) and len(txt) < 200:
                out.append((mo + s, txt))
        except UnicodeDecodeError:
            pass
        start = e + 1
    return out

def adrp_add_refs(target_va, segment='text'):
    """Find adrp+add pairs in .text materializing target (GHIDRA_BASE+va).
    Scans for adrp to the right page, then checks following few insns for
    add with the right low 12 bits."""
    fo, mo, sz = TEXT
    target_abs = GHIDRA_BASE + target_va
    page = target_abs & ~0xFFF
    lo = target_abs & 0xFFF
    w = text_words()
    hits = []
    for i, v in enumerate(w):
        if (v & 0x9F000000) == 0x90000000:  # ADRP
            pc = GHIDRA_BASE + i*4
            immlo = (v >> 29) & 3
            immhi = (v >> 5) & 0x7FFFF
            imm = (immhi << 2) | immlo
            if imm & (1 << 20):
                imm -= (1 << 21)
            if (pc & ~0xFFF) + (imm << 12) == page:
                rd = v & 0x1F
                # check next 6 insns for add rd2, rd, #lo or ldr with offset lo
                for j in range(1, 7):
                    if i + j >= len(w):
                        break
                    v2 = w[i+j]
                    # ADD (imm, 64-bit): 0x91000000, check Rn==rd, imm12==lo
                    if (v2 & 0xFF800000) == 0x91000000 and ((v2 >> 5) & 0x1F) == rd \
                       and ((v2 >> 10) & 0xFFF) == lo and ((v2 >> 22) & 1) == 0:
                        hits.append(i*4)
                        break
    return hits

def vtable_dump(vt_va, n=16):
    """Dump n vtable slots (data segment), as NSO-relative VAs."""
    out = []
    for k in range(n):
        ptr = u64(vt_va + 8*k)
        out.append(ptr - GHIDRA_BASE if ptr >= GHIDRA_BASE else ptr)
    return out

def str_at(va, maxlen=128):
    b = read(va, maxlen)
    e = b.find(b'\x00')
    return b[:e].decode('ascii', 'replace')

_RELOC_COUNT = _apply_relocs()

def name_getter_string(fn_va):
    """Nerve vtable slot-0 name getters typically do: adrp x0, page; add x0,x0,#lo; ret.
    Decode and return the string."""
    insns = dis(fn_va, 8, out=False)
    base = None
    for i in insns:
        m = i.mnemonic
        if m == 'adrp':
            base = int(i.op_str.split('#')[1], 16)
        elif m == 'add' and base is not None and i.op_str.startswith('x0'):
            lo = int(i.op_str.split('#')[1], 16)
            return str_at(base + lo - GHIDRA_BASE)
        elif m == 'ret':
            break
    return None
