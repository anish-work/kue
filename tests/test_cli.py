import pytest
import yaml
from typer.testing import CliRunner

from kue.cli.commands import app

runner = CliRunner()


@pytest.fixture
def kue(config_file):
    def _run(*args):
        return runner.invoke(app, ["--config", str(config_file), *args])
    return _run


def test_init_creates_and_refuses_overwrite(tmp_path):
    path = tmp_path / "new" / "config.yaml"
    assert runner.invoke(app, ["--config", str(path), "init"]).exit_code == 0
    assert path.exists()
    assert runner.invoke(app, ["--config", str(path), "init"]).exit_code == 1
    assert runner.invoke(app, ["--config", str(path), "init", "--force"]).exit_code == 0


def test_missing_config_exits_2(tmp_path):
    result = runner.invoke(app, ["--config", str(tmp_path / "none.yaml"), "status"])
    assert result.exit_code == 2
    assert "kue init" in result.output


def test_run_once_fails_clearly_without_gooey_secrets(tmp_path, config_dict):
    path = tmp_path / "gooey.yaml"
    path.write_text(yaml.safe_dump({**config_dict, "gooey": {"agent_example_id": "ex1"}}))
    result = runner.invoke(app, ["--config", str(path), "run-once"])
    assert result.exit_code == 2
    assert "GOOEY_API_KEY" in result.output


def test_status(kue):
    result = kue("status")
    assert result.exit_code == 0, result.output
    assert "KUE" in result.output and "ACTIVE" in result.output


def test_add_list_and_show(kue):
    assert kue("add", "Fix iOS keyboard", "--repo", "gooey-server", "--priority", "1").exit_code == 0
    listing = kue("tasks")
    assert "Fix iOS keyboard" in listing.output and "P1" in listing.output
    detail = kue("task", "1")
    assert "TASK_CREATED" in detail.output
    assert kue("task", "99").exit_code == 1


def test_run_once_dry_run_and_real(kue):
    kue("add", "Fix iOS keyboard", "--repo", "gooey-server")
    dry = kue("run-once", "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "Would create branch: kue/KUE-1-fix-ios-keyboard" in dry.output
    assert "DISCOVERED" in kue("tasks").output  # unchanged
    real = kue("run-once")
    assert real.exit_code == 0, real.output
    assert "QUEUED" in kue("tasks").output
    assert "EXECUTION_DEFERRED" in kue("logs").output


def test_pause_and_resume(kue):
    kue("add", "x", "--repo", "gooey-server")
    assert "PAUSED" in (kue("pause"), kue("status"))[1].output
    assert "Paused" in kue("run-once").output
    kue("resume")
    assert "ACTIVE" in kue("status").output


def test_approve_and_cancel_rules(kue):
    kue("add", "no repo task")  # no repo -> review
    kue("run-once")
    assert "REQUIRES_HUMAN_REVIEW" in kue("tasks").output
    assert kue("retry", "1").exit_code == 1  # wrong command for review state
    assert kue("approve", "1").exit_code == 0
    assert kue("cancel", "1").exit_code == 0
    assert kue("cancel", "1").exit_code == 1  # already terminal
    assert "CANCELLED" in kue("tasks", "--all").output
    assert "No tasks" in kue("tasks").output


def test_tasks_bad_status(kue):
    assert kue("tasks", "--status", "nonsense").exit_code == 1


def test_doctor_reports_repo(kue):
    result = kue("doctor")
    assert "repo gooey-server" in result.output
    assert "Claude Code CLI" in result.output
    assert result.exit_code == 0, result.output  # optional tools only warn
