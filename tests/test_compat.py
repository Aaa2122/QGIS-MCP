from qgis_agent_mcp.compat import unsafe_python_3d_creation


def test_qgis_344_python_3d_creation_is_blocked_on_windows():
    assert unsafe_python_3d_creation(34400, "win32")
    assert unsafe_python_3d_creation(34412, "win32")


def test_python_3d_creation_remains_available_elsewhere():
    assert not unsafe_python_3d_creation(34412, "linux")
    assert not unsafe_python_3d_creation(34399, "win32")
    assert not unsafe_python_3d_creation(34500, "win32")
