"""Build identity of the Switch subsdk, shared by the bundler and the client.

Why this exists: until 2026-09 the only identity the mod emitted was a
hand-maintained ``mod_ver = "smbwap-m4"`` and a boot banner that had not
changed since June.  Triaging a player's log meant dating the build by
which log strings it happened to contain.  Now ``switch-mod/cmake/
BuildId.cmake`` stamps a build id into subsdk9 at build time and the mod
sends it in the Hello ``mod_ver`` field; this module is the Python half:

* :func:`git_source_id` / :func:`bundle_source_id` compute the *source
  id* -- what the sources were.  ``scripts/install_apworld.py
  --bundle-mod`` writes :func:`bundle_source_id` into the bundle as
  ``switch-mod/VERSION`` (the wizard builds from that extracted bundle,
  which has no ``.git``, so the file is the only identity cmake can
  read there).
* :func:`expected_mod_source_id` tells the running client which source
  id a subsdk9 built from *this* apworld would carry.
* :func:`parse_mod_ver` / :func:`mod_ver_mismatch_warning` compare that
  against what the Switch reported, so the client log says "your
  subsdk9 is stale" instead of leaving it to be inferred.

Grammar (keep in sync with ``switch-mod/cmake/BuildId.cmake``)::

    build_id  := source "@" YYYYMMDD
    source    := [world_version "+"] "g" hash ["-dirty"]
               | world_version
               | "unknown"

Examples: ``g3f9a1c2-dirty@20260921`` (dev checkout),
``0.2.0+g3f9a1c2@20260921`` (release bundle), ``unknown@20260921``.
Anything without an ``@`` is a pre-stamping build (``smbwap-m4``).

stdlib only -- imported by the Kivy client and by the bundler script.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

UNKNOWN = "unknown"

# Dirty flag suffix the cmake script appends.  ``git describe --dirty``
# convention.
DIRTY_SUFFIX = "-dirty"

# The subtree whose uncommitted changes make a *mod* build dirty.  Both
# sides scope the dirty check here so a dev with an edited Python file
# but pristine switch-mod/ does not get flagged.
MOD_DIRTY_SCOPE = "switch-mod"

# Same character/length policy as BuildId.cmake: the id is embedded in a
# C string literal and shipped in a JSON field.
_SOURCE_ID_ALLOWED = re.compile(r"[^A-Za-z0-9._+-]")
MAX_SOURCE_ID_LEN = 40

# Zip-internal path of the VERSION file inside a --bundle-mod apworld.
# Mirrors the ``smbwonder/_setup/switch-mod/`` prefix install_apworld.py
# uses so extraction yields ``<bundle>/switch-mod/VERSION`` on disk, which
# is where cmake/BuildId.cmake looks (``${CMAKE_SOURCE_DIR}/VERSION``).
BUNDLE_VERSION_ARCNAME = "smbwonder/_setup/switch-mod/VERSION"

# Windows: keep the git subprocess from flashing a console under the
# windowed Launcher.  No-op elsewhere.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_GIT_TIMEOUT_S = 10.0


def sanitize_source_id(value: str) -> str:
    """Apply the cmake script's character/length policy; empty → unknown."""
    cleaned = _SOURCE_ID_ALLOWED.sub("", value.strip())[:MAX_SOURCE_ID_LEN]
    return cleaned or UNKNOWN


def _git(repo: Path, *args: str) -> str | None:
    """Run ``git -C repo <args>`` and return stripped stdout, or None on any
    failure (git missing, not a checkout, timeout).  Never raises."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def git_source_id(repo: Path, *, dirty_scope: str | None = MOD_DIRTY_SCOPE) -> str | None:
    """``g<short hash>[-dirty]`` for the checkout at ``repo``, or None when
    ``repo`` is not a git checkout / git is unavailable.

    ``dirty_scope`` limits the uncommitted-changes check to a subtree
    (default: ``switch-mod``); pass None for the whole tree.  Untracked
    files never count, matching the cmake side.
    """
    short = _git(repo, "rev-parse", "--short=7", "HEAD")
    if not short:
        return None
    source = f"g{short}"
    status_args = ["status", "--porcelain", "--untracked-files=no"]
    if dirty_scope:
        status_args += ["--", dirty_scope]
    status = _git(repo, *status_args)
    if status:
        source += DIRTY_SUFFIX
    return sanitize_source_id(source)


def read_world_version(apworld_dir: Path) -> str | None:
    """``world_version`` from ``<apworld_dir>/archipelago.json`` (the field
    the release workflow stamps from the tag), or None if unreadable."""
    try:
        data = json.loads((apworld_dir / "archipelago.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("world_version") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def bundle_source_id(repo_root: Path) -> str:
    """The source id ``install_apworld.py --bundle-mod`` writes into the
    bundle's ``switch-mod/VERSION``: ``<world_version>+g<hash>[-dirty]``
    when both are known, else whichever is, else ``unknown``.

    In the release workflow this runs after the tag is stamped into
    archipelago.json, so a release bundle reads e.g. ``0.2.0+g3f9a1c2``.
    A dev ``--bundle-mod`` run keeps whatever world_version the checkout
    has plus the live hash, which is still unambiguous.
    """
    world_version = read_world_version(repo_root / "apworld" / "smbw_archipelago")
    git_id = git_source_id(repo_root)
    if world_version and git_id:
        return sanitize_source_id(f"{world_version}+{git_id}")
    return sanitize_source_id(world_version or git_id or UNKNOWN)


def _find_apworld_zip(start: Path) -> Path | None:
    """Walk up from ``start`` to the enclosing ``.apworld`` zip file, if any.

    Same logic as ``_setup.build._find_apworld_zip``; duplicated (it is
    six lines) so the client does not import the whole build module just
    to learn its own version.
    """
    cur = start
    for _ in range(10):
        if cur.suffix == ".apworld" and cur.is_file():
            return cur
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent
    return None


def read_bundle_version(apworld_zip: Path) -> str | None:
    """The ``VERSION`` file inside a ``--bundle-mod`` apworld zip, or None
    if the zip predates bundling the file / is unreadable."""
    try:
        with zipfile.ZipFile(apworld_zip) as zf:
            raw = zf.read(BUNDLE_VERSION_ARCNAME)
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    first = raw.decode("utf-8", errors="replace").splitlines()
    return sanitize_source_id(first[0]) if first else None


def expected_mod_source_id(setup_root: Path | None = None) -> str | None:
    """Source id a subsdk9 built from *this* apworld install would carry.

    * Dev checkout (``<repo>/.git`` exists, dir or worktree file): the
      live ``g<hash>[-dirty]`` -- the same thing BuildId.cmake computes.
    * Packaged install (running out of a ``.apworld`` zip): the bundled
      ``VERSION`` file, which is what the wizard's cmake run reads.
    * Neither (apworld extracted to a plain folder): None -- nothing to
      compare against, so the client skips the staleness check.

    ``setup_root`` defaults to this package's ``_setup/`` directory; tests
    pass a synthetic one.
    """
    root = setup_root if setup_root is not None else Path(__file__).resolve().parent
    # <repo>/apworld/smbw_archipelago/_setup -> parents[2] == <repo>
    repo = root.parents[2] if len(root.parents) > 2 else None
    if repo is not None and (repo / ".git").exists():
        return git_source_id(repo)
    apworld_zip = _find_apworld_zip(root)
    if apworld_zip is None:
        return None
    return read_bundle_version(apworld_zip)


@dataclass(frozen=True)
class ParsedModVer:
    """One Hello ``mod_ver`` value taken apart."""
    raw: str
    source: str          # source id with the dirty suffix stripped
    dirty: bool
    build_date: str      # "YYYYMMDD" or "" when absent
    stamped: bool        # False for pre-build-id mods ("smbwap-m4")

    @property
    def known(self) -> bool:
        """True when the source id names real sources (not ``unknown``)."""
        return self.stamped and self.source != UNKNOWN


def parse_mod_ver(mod_ver: str) -> ParsedModVer:
    raw = (mod_ver or "").strip()
    if "@" not in raw:
        return ParsedModVer(raw=raw, source=raw, dirty=False, build_date="", stamped=False)
    source, _, date = raw.partition("@")
    dirty = source.endswith(DIRTY_SUFFIX)
    if dirty:
        source = source[: -len(DIRTY_SUFFIX)]
    return ParsedModVer(raw=raw, source=source, dirty=dirty, build_date=date, stamped=True)


def source_key(source_id: str) -> str:
    """Comparison key: the source id minus its dirty flag.  The flag can
    flip between the build and the run (dev edits a file after building),
    which is not the staleness signal we are after."""
    s = (source_id or "").strip()
    if s.endswith(DIRTY_SUFFIX):
        s = s[: -len(DIRTY_SUFFIX)]
    return s


def mod_ver_mismatch_warning(reported: str, expected: str | None) -> str | None:
    """A warning line when the Switch's ``mod_ver`` does not match what a
    subsdk9 built from this apworld would report, else None.

    Quiet cases (None): ``expected`` unknown (plain-folder install), the
    mod's own id is ``unknown`` (built with no git and no VERSION file --
    nothing to compare), or the two agree modulo the dirty flag.
    """
    parsed = parse_mod_ver(reported)
    if not parsed.stamped:
        return (
            f"Switch mod reports mod_ver={parsed.raw!r}, a build from before "
            f"build-id stamping (pre-2026-09-21) -- it is missing every fix "
            f"since then.  Rebuild + redeploy subsdk9 from this apworld "
            f"(/setup), and make sure the emulator loads the new copy."
        )
    if not expected or not parsed.known:
        return None
    if source_key(expected) == source_key(parsed.source):
        return None
    return (
        f"Switch mod build {parsed.raw!r} does not match this apworld "
        f"({expected!r}).  The emulator is probably loading a stale "
        f"subsdk9 -- rebuild + redeploy (/setup) and check the mod folder "
        f"the emulator actually reads."
    )


def _main(argv: list[str]) -> int:  # pragma: no cover - manual helper
    """``python -m apworld.smbw_archipelago._setup.build_id [repo]`` prints
    the bundle source id for a checkout; handy when checking what a
    ``--bundle-mod`` run would stamp."""
    repo = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parents[3]
    print(bundle_source_id(repo))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv))
