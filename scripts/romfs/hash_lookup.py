"""MurmurHash3 x86_32 seed 0 over candidate GameData names; match against GameDataList.

This is THE GameData flag-name hash function for SMBW: murmur3 x86_32, seed 0,
over the flag's name as UTF-8 bytes (strlen length — the NUL is NOT hashed).
For a Struct member the hashed name is the *dotted* full name
("WorldMapCloudPackunVanishInfo.IsVanishWa"), not the bare member name.

Validated: IsChangeEnvEnterKoopaCastle -> 0xe02a5e43 (recovered independently
from a W6-cutscene save diff), and every WorldMapCloudPackunVanishInfo member
verifies against its full-name hash.

Usage:
    python hash_lookup.py names.txt [GameDataList.json]
where names.txt is one candidate name per line. The GameDataList.json path
defaults to $SMBW_GDL, else ./GameDataList.json (produce it from the RomFS with
`python byml_parse.py GameDataList.Product.100.byml GameDataList.json`).
"""
import json
import os
import struct
import sys


def murmur3_32(data: bytes, seed: int = 0) -> int:
    c1, c2 = 0xCC9E2D51, 0x1B873593
    h = seed
    n = len(data)
    for i in range(0, n - n % 4, 4):
        k = struct.unpack_from("<I", data, i)[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF
    k = 0
    tail = data[n - n % 4 :]
    if len(tail) >= 3:
        k ^= tail[2] << 16
    if len(tail) >= 2:
        k ^= tail[1] << 8
    if len(tail) >= 1:
        k ^= tail[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
    h ^= n
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


def load_index(gdl_path):
    """hash -> (category, position, entry) over every GameDataList row."""
    gdl = json.load(open(gdl_path))
    index = {}
    for cat, entries in gdl["Data"].items():
        for i, e in enumerate(entries):
            index[e["Hash"]] = (cat, i, e)
    return index


if __name__ == "__main__":
    gdl_path = sys.argv[2] if len(sys.argv) > 2 else os.environ.get(
        "SMBW_GDL", "GameDataList.json"
    )
    index = load_index(gdl_path)

    names = [line.strip() for line in open(sys.argv[1]) if line.strip()]
    for name in names:
        h = murmur3_32(name.encode())
        hit = index.get(h)
        if hit:
            cat, i, e = hit
            extra = {k: v for k, v in e.items() if k != "Hash"}
            print(f"{name}  0x{h:08x}  -> {cat}[{i}]  {extra}")
        else:
            print(f"{name}  0x{h:08x}  -> (not in GameDataList)")
