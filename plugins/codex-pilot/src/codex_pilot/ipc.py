"""Compatibility alias for :mod:`codex_desktop_core.ipc`."""

import sys as _sys

from codex_desktop_core import ipc as _module

_sys.modules[__name__] = _module
