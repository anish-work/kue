import subprocess

from kue.coding.workspace import branch_name_for, inspect_repository


def test_clean_repo_is_ready(git_repo):
    state = inspect_repository(git_repo)
    assert state.problem is None
    assert state.branch == "main"


def test_modified_file_is_dirty(git_repo):
    (git_repo / "README.md").write_text("changed\n")
    state = inspect_repository(git_repo)
    assert state.dirty and "README.md" in state.dirty_files
    assert "uncommitted" in state.problem


def test_untracked_file_is_dirty(git_repo):
    (git_repo / "notes.txt").write_text("wip\n")
    assert inspect_repository(git_repo).dirty


def test_missing_path(tmp_path):
    state = inspect_repository(tmp_path / "nope")
    assert not state.exists and "does not exist" in state.problem


def test_not_a_git_repo(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    state = inspect_repository(folder)
    assert state.exists and not state.is_git
    assert "Not a git repository" in state.problem


def test_detached_head(git_repo):
    subprocess.run(["git", "-C", str(git_repo), "checkout", "--detach"], check=True, capture_output=True)
    assert "detached" in inspect_repository(git_repo).problem


def test_branch_names():
    assert branch_name_for(123, "Fix iOS keyboard overlap!") == "kue/KUE-123-fix-ios-keyboard-overlap"
    assert branch_name_for(7, "???") == "kue/KUE-7-task"
    long = branch_name_for(1, "a" * 30 + " " + "b" * 30)
    assert len(long.split("-", 2)[2]) <= 40 and not long.endswith("-")
