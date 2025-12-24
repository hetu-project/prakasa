"""Compatibility package to satisfy build tooling when project.name == "prakasa".

This module re-exports the existing `parallax` package so that tools which
expect a top-level package matching the project name (e.g. Poetry editable
build) can find a package named `prakasa`.

Keep this file minimal and tolerant to import-time issues.
"""
try:
    # Import the real implementation package and re-export its symbols.
    _parallax = __import__("parallax")
    from parallax import *  # re-export
    __all__ = getattr(_parallax, "__all__", [])
except Exception:
    # If import fails during build-time, provide a safe fallback.
    __all__ = []
