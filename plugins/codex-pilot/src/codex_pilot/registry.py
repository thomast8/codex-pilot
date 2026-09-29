"""Compatibility alias for :mod:`codex_desktop_core.registry`."""

import sys as _sys

from codex_desktop_core import registry as _module

_sys.modules[__name__] = _module
