"""Tests for config_registry.py project registry helpers."""

import json
from unittest import mock

from lanegate.config_registry import registry_add


class TestRegistryAdd:
    """Tests for registry_add() — global project registration."""

    def test_registry_add_creates_entry(self, tmp_path):
        """registry_add writes an entry for the project."""
        fake_registry = tmp_path / "projects.json"
        with (
            mock.patch("lanegate.config_registry._REGISTRY_FILE", fake_registry),
            mock.patch("lanegate.config_registry._REGISTRY_DIR", tmp_path),
        ):
            registry_add(tmp_path)

        data = json.loads(fake_registry.read_text())
        paths = [e["path"] for e in data]
        assert str(tmp_path.resolve()) in paths

    def test_registry_add_is_idempotent(self, tmp_path):
        """Calling registry_add twice does not create duplicate entries."""
        fake_registry = tmp_path / "projects.json"
        with (
            mock.patch("lanegate.config_registry._REGISTRY_FILE", fake_registry),
            mock.patch("lanegate.config_registry._REGISTRY_DIR", tmp_path),
        ):
            registry_add(tmp_path)
            registry_add(tmp_path)

        data = json.loads(fake_registry.read_text())
        matching = [e for e in data if e["path"] == str(tmp_path.resolve())]
        assert len(matching) == 1


