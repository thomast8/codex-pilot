"""Compatibility alias for :mod:`codex_desktop_core.threads`."""

import sys as _sys

from codex_desktop_core import threads as _module

_sys.modules[__name__] = _module
