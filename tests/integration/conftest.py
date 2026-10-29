import pytest

from tests.integration.harness import q_binary


def pytest_collection_modifyitems(config, items):
    if q_binary():
        return
    skip = pytest.mark.skip(reason="q is not on PATH: the integration tests run the generated q")
    for item in items:
        if "integration" in item.nodeid:
            item.add_marker(skip)
