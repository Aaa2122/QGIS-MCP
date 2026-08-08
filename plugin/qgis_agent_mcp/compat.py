from __future__ import annotations


def unsafe_python_3d_creation(version_int, platform):
    """Return whether PyQGIS 3D-view creation is process-unsafe."""
    return str(platform) == "win32" and 34400 <= int(version_int) < 34500
