"""Compatibility alias for :mod:`codex_desktop_core.worktrees`."""

import sys as _sys

from codex_desktop_core import worktrees as _module

_sys.modules[__name__] = _module
