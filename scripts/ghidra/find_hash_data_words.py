# -*- coding: utf-8 -*-
# Ghidra script -- SMBW Archipelago, find container-C descriptor records.
#
# The container-C typed objects (badge bitfield 0x105df820, per-course
# Wonder Seed bitfield 0x60458608, etc.) are built at save-data init from
# a DESCRIPTOR TABLE -- an array of records like {hash, bit_count/size,
# type, ...}.  That's why find_hash_immediate_loads only found code that
# READS/WRITES these hashes, never a constructor: the hash lives in the
# table as a DATA word, not a code immediate.
#
# This script searches ALL memory for the little-endian 4-byte data words
# of the target hashes and dumps the surrounding bytes (as u32 LE words
# with offsets) so we can read the descriptor record.  Comparing the
# Wonder Seed record to the badge record (known 59 bits -> serialized as
# u64/8 bytes) tells us the WS declared width directly:
#
#   * If WS's size field == badge's size field (both ~u64)  -> Q3 = data[2..3]
#     is a mirror, does NOT persist; cap AP packing to 64 bits.
#   * If WS's size field is larger (>=80 bits / >=12 bytes) -> Q3 = data[2]
#     is genuine storage for courses 64-79 and DOES persist.
#
# OUTPUT -> OUTPUT_PATH inside the repo.

# @author smbwap
# @category SMBW Archipelago
# @menupath

from __future__ import print_function

OUTPUT_PATH = (
    r"C:\Users\maxwe\Documents\smwonder_archipelago\.claude\worktrees"
    r"\bridge-cse_01KtYB9wuC3NNZhW48vZ89Zd\scripts\ghidra"
    r"\out_hash_data_words.txt")

TARGETS = [
    (0x60458608, "per-course Wonder Seed bitfield (container C)"),
    (0x105df820, "badge owned bitfield (container C; ~59 bits, file 0x0EA0)"),
    (0x6d1b5c25, "badge UI-slot bitmap (container C aux; file 0x1204)"),
    (0xe48a1168, "second badge category (paired with 0x105df820 in reader)"),
    (0x580b7eb4, "per-course status enum (container D-ish, gmd+0x2c0)"),
]

CONTEXT_BEFORE = 0x40
CONTEXT_AFTER = 0x40

_OUT = {"fh": None}


def emit(s=""):
    print(s)
    fh = _OUT["fh"]
    if fh is not None:
        try:
            fh.write(s)
            fh.write("\n")
        except Exception:
            pass


def _le32_bytes(val):
    import jarray
    raw = [val & 0xFF, (val >> 8) & 0xFF, (val >> 16) & 0xFF, (val >> 24) & 0xFF]
    signed = [(b - 256) if b > 127 else b for b in raw]
    return jarray.array(signed, 'b')


def _read_u8(addr):
    try:
        return getByte(addr) & 0xFF
    except Exception:
        return None


def _dump_context(center_addr, hash_val):
    """Print a hex + u32 dump of [center-CONTEXT_BEFORE, center+CONTEXT_AFTER)."""
    af = currentProgram.getAddressFactory()
    base = center_addr.getOffset()
    start = base - CONTEXT_BEFORE
    total = CONTEXT_BEFORE + CONTEXT_AFTER

    raw = []
    for i in range(total):
        a = af.getAddress("%x" % (start + i))
        b = _read_u8(a)
        raw.append(b)

    # u32 LE rendering, 4 per line, with offset relative to center.
    emit("    u32 words (offset relative to hash word @ +0x00):")
    i = 0
    while i + 4 <= total:
        words = []
        for w in range(4):
            j = i + w * 4
            if j + 4 > total:
                break
            bs = raw[j:j + 4]
            if None in bs:
                words.append("????????")
            else:
                val = bs[0] | (bs[1] << 8) | (bs[2] << 16) | (bs[3] << 24)
                words.append("%08x" % val)
        rel = (start + i) - base
        sign = "+" if rel >= 0 else "-"
        emit("      %s0x%02x: %s" % (sign, abs(rel), "  ".join(words)))
        i += 16


def run():
    emit("SMBW Archipelago: find_hash_data_words.py")
    mem = currentProgram.getMemory()
    emit("  memory blocks:")
    for blk in mem.getBlocks():
        emit("    %-16s %s - %s  (init=%s, r=%s w=%s x=%s)"
             % (blk.getName(), blk.getStart(), blk.getEnd(),
                blk.isInitialized(), blk.isRead(), blk.isWrite(),
                blk.isExecute()))
    emit("")

    af = currentProgram.getAddressFactory()
    minA = mem.getMinAddress()

    for hash_val, label in TARGETS:
        emit("============================================================")
        emit("0x%08x  %s" % (hash_val, label))
        emit("============================================================")
        pattern = _le32_bytes(hash_val)
        # findBytes(start, byteString, matchLimit) -> Address[]
        found = []
        start = minA
        guard = 0
        while start is not None and guard < 2000:
            guard += 1
            try:
                a = currentProgram.getMemory().findBytes(
                    start, pattern, None, True, monitor)
            except Exception as e:
                emit("    (findBytes error: %s)" % e)
                a = None
            if a is None:
                break
            found.append(a)
            nxt = a.getOffset() + 1
            try:
                start = af.getAddress("%x" % nxt)
            except Exception:
                break
            if monitor.isCancelled():
                break
        if not found:
            emit("  (no data-word occurrences found)")
            emit("")
            continue
        emit("  %d occurrence(s):" % len(found))
        for a in found:
            blk = currentProgram.getMemory().getBlock(a)
            blk_name = blk.getName() if blk is not None else "?"
            is_exec = blk.isExecute() if blk is not None else False
            emit("")
            emit("  @ %s  block=%s%s"
                 % (a, blk_name, "  [EXEC/code-imm]" if is_exec else ""))
            if is_exec:
                emit("    (in executable block -- likely a code immediate, "
                     "not a descriptor; skipping dump)")
                continue
            _dump_context(a, hash_val)
        emit("")
    emit("Done.")


def main():
    try:
        _OUT["fh"] = open(OUTPUT_PATH, "w")
    except Exception as e:
        print("WARN: could not open output: %s" % e)
        _OUT["fh"] = None
    try:
        run()
    finally:
        if _OUT["fh"] is not None:
            try:
                _OUT["fh"].flush()
                _OUT["fh"].close()
            except Exception:
                pass
            print("Wrote full output to: %s" % OUTPUT_PATH)


main()
