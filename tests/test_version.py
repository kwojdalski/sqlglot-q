"""The version is written twice, in pyproject.toml and version.py, and the
release workflow publishes whatever the tag says: the three must agree."""

from importlib.metadata import version

from sqlglot_q import __version__


def test_package_and_module_versions_agree():
    assert version("sqlglot-q") == __version__
