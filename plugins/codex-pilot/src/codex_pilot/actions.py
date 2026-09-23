"""Compatibility alias for :mod:`codex_desktop_core.actions`."""

import sys as _sys

from codex_desktop_core import actions as _module

_sys.modules[__name__] = _module
