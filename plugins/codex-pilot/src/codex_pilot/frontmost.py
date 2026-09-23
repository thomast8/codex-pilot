"""Compatibility alias for :mod:`codex_desktop_core.frontmost`."""

import sys as _sys

from codex_desktop_core import frontmost as _module

_sys.modules[__name__] = _module
