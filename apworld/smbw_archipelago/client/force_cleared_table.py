"""Secret-exit "replay" courses gated on the transient IsInClearedCourse flag.

A handful of Search-Party / Royal-Seed courses only spawn their **secret goal**
(and remove the breakable-stone wall blocking the secret route) once the game
considers the course *already cleared* -- driven by the transient GameData bool
``IsInClearedCourse`` (murmur ``0xbef2db36``, ``SaveFileIndex -1``).  In the
vanilla linear flow the game sets that flag true when you re-enter a cleared
course; in **open-world mode** the linear world-clear flow is bypassed, so the
flag never flips, the secret path never appears, and the ``SECRET_EXIT`` check
is unreachable (datamined from ``BancMapUnit/Course551_Course.bcett.byml``,
2026-06-30).

The bridge sends the Switch a bitmask of which of these courses to force-clear;
the Switch (``kForceClearedCourses`` in ``switch-mod/src/main.cpp``) maps each
bit to an in-game ``(world_val, CourseInfo.CourseId enum)`` identity and writes
``IsInClearedCourse`` at scene-load so the secret path spawns and the player
reaches the secret goal naturally (the ``SECRET_EXIT`` check then fires the
normal ``goal_id == 1`` way).

**Bit order here MUST match ``kForceClearedCourses`` on the Switch side.**

Each entry is ``(secret_exit_location, normal_exit_location | None)``.  The
gating rule (:meth:`SMBWContext._recompute_force_cleared_mask`): open-world AND
(the course has **no** ``NORMAL_EXIT`` location -> force-clear always; else
force-clear only once that ``NORMAL_EXIT`` location has been checked, so the
player still plays the normal exit first).  Both current courses award a Royal
Seed on the normal goal (``goal_id 0`` -> ``PALACE_CLEAR``), not a
``NORMAL_EXIT`` check, so both are ``None`` -> always force-clear in open-world.
"""

from __future__ import annotations

#: Bit N -> (secret-exit location name, normal-exit location name or ``None``).
#: MUST stay in sync (order + count) with ``kForceClearedCourses`` in
#: ``switch-mod/src/main.cpp``.  The location names must match
#: ``data/locations.json`` character-for-character.
FORCE_CLEARED_COURSES: list[tuple[str, "str | None"]] = [
    # bit 0: Operation Poplin Rescue -- W5 Fungi Mines finale (Course551,
    #        world_val 6, CourseInfo.CourseId enum "Course11" 0xcd7c09bb).
    ("W5: Operation Poplin Rescue - Secret Exit", None),
    # bit 1: Royal Seed Mansion -- W3 Shining Falls (Course531, world_val 4,
    #        CourseInfo.CourseId enum "Course61" 0x37d76dc1).
    ("W3: Royal Seed Mansion - Secret Exit", None),
]
