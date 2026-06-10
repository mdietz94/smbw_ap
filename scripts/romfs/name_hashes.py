"""Recover GameData flag NAMES from their hashes by brute-matching a string corpus.

Given a list of hashes (one 0xHHHHHHHH per line) and a corpus of candidate
identifier strings, murmur3-hash every candidate and look for a match. Also
composes Struct members as dotted full names ("StructName.MemberName") and
verifies them against the GameDataList's recorded member-value hash.

Build the corpus with build_corpus.py (harvests identifier-like runs from the
decompressed NSO + the RomFS text-bearing files), or pass any text file(s).

Usage:
    python name_hashes.py hashes.txt corpus.txt [more_corpus...]
GameDataList.json path comes from $SMBW_GDL (default ./GameDataList.json).
"""
import json
import os
import re
import sys

from hash_lookup import murmur3_32

IDENT = re.compile(rb"[A-Za-z][A-Za-z0-9_.]{2,63}")


def corpus_from(path):
    data = open(path, "rb").read()
    return set(m.group(0).decode() for m in IDENT.finditer(data))


def main():
    gdl_path = os.environ.get("SMBW_GDL", "GameDataList.json")
    gdl = json.load(open(gdl_path))

    hashes = [int(x, 16) for x in re.findall(r"0x[0-9a-fA-F]{8}", open(sys.argv[1]).read())]

    names = set()
    for p in sys.argv[2:]:
        names |= corpus_from(p)
    print(f"corpus: {len(names)} candidate strings", file=sys.stderr)

    h2n = {}
    for n in names:
        h2n.setdefault(murmur3_32(n.encode()), n)

    # struct composition: member-name hash -> member name, then dotted full names
    for s in gdl["Data"].get("Struct", []):
        sname = h2n.get(s["Hash"])
        for ent in s["DefaultValue"]:
            mname = h2n.get(ent["Hash"])
            if sname and mname:
                full = f"{sname}.{mname}"
                if murmur3_32(full.encode()) == ent["Value"]:
                    h2n[ent["Value"]] = full

    index = {}
    for cat, entries in gdl["Data"].items():
        for i, e in enumerate(entries):
            index[e["Hash"]] = (cat, i, e.get("SaveFileIndex"))

    named = sum(1 for h in hashes if h in h2n)
    print(f"named {named}/{len(hashes)} hashes", file=sys.stderr)
    for h in hashes:
        cat, i, sf = index.get(h, ("?", -1, "?"))
        print(f"0x{h:08x}  {cat}[{i}] save={sf}  {h2n.get(h, '???')}")


if __name__ == "__main__":
    main()
