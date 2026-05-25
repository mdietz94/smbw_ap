"""Hooks for adding/modifying AP options before SMBWonder's options
dataclass is built.  Both hooks currently no-op; populate `options`
in `before_options_defined` to register new YAML-configurable options.
"""


def before_options_defined(options: dict) -> dict:
    return options


def after_options_defined(options: dict) -> dict:
    return options
