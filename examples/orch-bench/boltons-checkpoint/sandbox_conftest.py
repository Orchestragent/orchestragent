"""Frozen evaluation overlay: explicit upstream tests incompatible with the sandbox."""

import pytest

UNSUPPORTED = {
    "tests/test_pathutils.py::test_shrinkuser_root_home": "requires subprocess execution",
    "tests/test_socketutils.py::test_socketutils_netstring": "requires TCP listen",
    "tests/test_socketutils.py::test_socketutils_netstring_timeout": "requires TCP listen",
}


def pytest_collection_modifyitems(items):
    for item in items:
        if item.nodeid in UNSUPPORTED:
            item.add_marker(pytest.mark.skip(reason="sandbox: " + UNSUPPORTED[item.nodeid]))
