"""Compatibility alias for :mod:`codex_desktop_core.instances`."""

import sys as _sys

from codex_desktop_core import instances as _module

_sys.modules[__name__] = _module
