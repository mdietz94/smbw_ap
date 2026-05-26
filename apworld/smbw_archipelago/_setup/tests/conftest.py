"""Pytest conftest stub for the _setup tests.

Currently empty — the root `conftest.py` handles vendor/Archipelago
sys.path setup, and the per-test setup is handled by pytest fixtures
defined inline in each test module.

DEV-ENV NOTE: if your `vendor/Archipelago/custom_worlds/` contains
BOTH the source `smbw_archipelago` (as junction or copy) AND a
`smbw_archipelago.apworld` zip built by `scripts/build_apworld.py`,
AP's autodiscover registers SMBWonder twice and collection fails with:

    RuntimeError: Game SMBWonder already registered in <zip>
                  when attempting to register from <source>

Fix by removing the zip:

    Remove-Item vendor\\Archipelago\\custom_worlds\\smbw_archipelago.apworld

Or by removing the source junction and using the zip-only setup. The
two cannot coexist.
"""
