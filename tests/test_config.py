from pathlib import Path

import pytest
import yaml

from kue.config.models import AppConfig
from kue.config.settings import ConfigError, load_config, template_text

ROOT = Path(__file__).resolve().parents[1]


def test_template_is_valid_config():
    cfg = AppConfig.model_validate(yaml.safe_load(template_text()))
    assert "gooey-server" in cfg.repositories
    assert cfg.autonomy.merge_mode == "when_approved"
    assert cfg.workspace.dirty_tree_action == "skip"


def test_root_example_matches_packaged_template():
    assert (ROOT / "config.example.yaml").read_text() == template_text()


def test_paths_are_expanded():
    cfg = AppConfig.model_validate({"repositories": {"r": {"path": "~/code/r"}}})
    assert not str(cfg.repositories["r"].path).startswith("~")
    assert "~" not in str(cfg.database.path)


def test_blank_test_command_becomes_none():
    cfg = AppConfig.model_validate({"repositories": {"r": {"path": "/x", "test_command": "  "}}})
    assert cfg.repositories["r"].test_command is None


def test_unknown_keys_are_rejected(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("autonomy:\n  auto_merge: true\n")
    with pytest.raises(ConfigError, match="auto_merge"):
        load_config(path)


def test_invalid_repo_name_rejected():
    with pytest.raises(ValueError):
        AppConfig.model_validate({"repositories": {"bad name": {"path": "/x"}}})


def test_missing_file_mentions_init(tmp_path):
    with pytest.raises(ConfigError, match="kue init"):
        load_config(tmp_path / "missing.yaml")


def test_non_mapping_rejected(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_resolve_repository(config):
    assert config.resolve_repository("gooey-server") is not None
    assert config.resolve_repository("old-repo") is None  # disabled
    assert config.resolve_repository("nope") is None
    assert config.resolve_repository(None) is None
