"""Compatibility alias for :mod:`codex_desktop_core.snapshot`."""

import sys as _sys

from codex_desktop_core import snapshot as _module

_sys.modules[__name__] = _module
