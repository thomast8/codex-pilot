"""Compatibility alias for :mod:`codex_desktop_core.resume`."""

import sys as _sys

from codex_desktop_core import resume as _module

_sys.modules[__name__] = _module
