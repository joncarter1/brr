"""Tests for git-based project sync (cluster.py + templates.py)."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import click
import pytest

from brr.cluster import _get_git_info, _validate_git_for_sync, _staging_project_map
from brr.templates import inject_brr_infra, prepare_staging


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(cwd, *args):
    """Run a git command in the given directory."""
    subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True, check=True,
    )


def _init_repo(tmp_path, remote_url="git@github.com:user/repo.git", commit=True):
    """Create a git repo with a remote and an initial commit. Returns repo path."""
    repo = tmp_path / "myproject"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", remote_url)
    if commit:
        (repo / "README.md").write_text("hello")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "init")
    return repo


def _init_repo_with_upstream(tmp_path, name="myproject"):
    """Create a bare remote + cloned working repo. Returns (bare, working)."""
    bare = tmp_path / f"{name}-bare.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", "main")

    working = tmp_path / name
    subprocess.run(
        ["git", "clone", str(bare), str(working)],
        capture_output=True, text=True, check=True,
    )
    _git(working, "config", "user.email", "test@test.com")
    _git(working, "config", "user.name", "Test")
    (working / "README.md").write_text("hello")
    _git(working, "add", "README.md")
    _git(working, "commit", "-m", "init")
    _git(working, "push", "-u", "origin", "main")

    # Switch remote to SSH URL (clone used file path)
    _git(working, "remote", "set-url", "origin", "git@github.com:user/myproject.git")

    return bare, working


# ---------------------------------------------------------------------------
# _get_git_info
# ---------------------------------------------------------------------------

class TestGetGitInfo:

    def test_returns_info_for_git_repo(self, tmp_path):
        repo = _init_repo(tmp_path)
        info = _get_git_info(repo)
        assert info is not None
        assert info["remote_url"] == "git@github.com:user/repo.git"
        assert info["branch"] == "main"
        assert len(info["commit"]) == 40  # full SHA
        assert info["repo_name"] == "myproject"
        assert info["project_path"] == str(repo)

    def test_returns_none_for_non_git_dir(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert _get_git_info(plain) is None

    def test_returns_none_for_repo_without_remote(self, tmp_path):
        repo = tmp_path / "norepo"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "config", "user.email", "t@t.com")
        _git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("x")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-m", "init")
        assert _get_git_info(repo) is None

    def test_returns_correct_branch(self, tmp_path):
        repo = _init_repo(tmp_path)
        _git(repo, "checkout", "-b", "feature")
        info = _get_git_info(repo)
        assert info["branch"] == "feature"


# ---------------------------------------------------------------------------
# _validate_git_for_sync
# ---------------------------------------------------------------------------

class TestValidateGitForSync:

    def test_passes_clean_repo(self, tmp_path):
        bare, working = _init_repo_with_upstream(tmp_path)
        git_info = _get_git_info(working)
        config = {"GITHUB_SSH_KEY": "/path/to/key"}
        # Should not raise
        _validate_git_for_sync(working, git_info, config)

    def test_fails_dirty_working_tree(self, tmp_path):
        bare, working = _init_repo_with_upstream(tmp_path)
        (working / "dirty.txt").write_text("uncommitted")
        _git(working, "add", "dirty.txt")
        git_info = _get_git_info(working)
        config = {"GITHUB_SSH_KEY": "/path/to/key"}
        with pytest.raises(click.UsageError, match="uncommitted changes"):
            _validate_git_for_sync(working, git_info, config)

    def test_fails_https_remote(self, tmp_path):
        bare, working = _init_repo_with_upstream(tmp_path)
        _git(working, "remote", "set-url", "origin", "https://github.com/user/repo.git")
        git_info = _get_git_info(working)
        config = {"GITHUB_SSH_KEY": "/path/to/key"}
        with pytest.raises(click.UsageError, match="not SSH"):
            _validate_git_for_sync(working, git_info, config)

    def test_fails_unpushed_commits(self, tmp_path):
        bare, working = _init_repo_with_upstream(tmp_path)
        (working / "new.txt").write_text("new")
        _git(working, "add", "new.txt")
        _git(working, "commit", "-m", "local only")
        git_info = _get_git_info(working)
        config = {"GITHUB_SSH_KEY": "/path/to/key"}
        with pytest.raises(click.UsageError, match="not pushed"):
            _validate_git_for_sync(working, git_info, config)

    def test_fails_missing_ssh_key(self, tmp_path):
        bare, working = _init_repo_with_upstream(tmp_path)
        git_info = _get_git_info(working)
        config = {}  # No GITHUB_SSH_KEY
        with pytest.raises(click.UsageError, match="GITHUB_SSH_KEY"):
            _validate_git_for_sync(working, git_info, config)

    def test_collects_multiple_errors(self, tmp_path):
        bare, working = _init_repo_with_upstream(tmp_path)
        # Dirty + no SSH key
        (working / "dirty.txt").write_text("x")
        _git(working, "add", "dirty.txt")
        git_info = _get_git_info(working)
        config = {}
        with pytest.raises(click.UsageError) as exc_info:
            _validate_git_for_sync(working, git_info, config)
        msg = str(exc_info.value)
        assert "uncommitted" in msg
        assert "GITHUB_SSH_KEY" in msg


# ---------------------------------------------------------------------------
# inject_brr_infra — git_info handling
# ---------------------------------------------------------------------------

class TestInjectBrrInfra:

    def test_with_git_info_writes_repo_info_and_adds_setup_command(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        config = {"setup_commands": [], "file_mounts": {}}
        git_info = {
            "remote_url": "git@github.com:user/repo.git",
            "branch": "main",
            "commit": "abc123",
            "repo_name": "repo",
            "project_path": "/some/path",
        }
        inject_brr_infra(config, staging, git_info=git_info)

        # repo_info.json written to staging
        repo_info_path = staging / "repo_info.json"
        assert repo_info_path.exists()
        written = json.loads(repo_info_path.read_text())
        assert written["remote_url"] == "git@github.com:user/repo.git"
        assert written["commit"] == "abc123"

        # sync-repo.sh appended to setup_commands
        assert any("sync-repo.sh" in cmd for cmd in config["setup_commands"])

        # No _project/ in file_mounts
        for mount in config["file_mounts"]:
            assert "_project" not in mount

    def test_without_git_info_no_repo_sync(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        config = {"setup_commands": [], "file_mounts": {}}
        inject_brr_infra(config, staging, git_info=None)

        assert not (staging / "repo_info.json").exists()
        assert not any("sync-repo.sh" in cmd for cmd in config["setup_commands"])


# ---------------------------------------------------------------------------
# prepare_staging — sync-repo.sh copied
# ---------------------------------------------------------------------------

class TestPrepareStaging:

    def test_copies_sync_repo_sh(self, tmp_path):
        with patch("brr.templates.STATE_DIR", tmp_path), \
             patch("brr.templates.CONFIG_PATH", tmp_path / "config.env"):
            staging = prepare_staging("test-cluster", "aws")
            assert (staging / "sync-repo.sh").exists()
            content = (staging / "sync-repo.sh").read_text()
            assert "repo_info.json" in content
            assert "git clone" in content


# ---------------------------------------------------------------------------
# _staging_project_map — scanning repo_info.json
# ---------------------------------------------------------------------------

class TestStagingProjectMap:

    def test_finds_repo_info(self, tmp_path):
        staging = tmp_path / "staging"
        cluster_dir = staging / "my-cluster"
        cluster_dir.mkdir(parents=True)
        (cluster_dir / "repo_info.json").write_text(json.dumps({
            "remote_url": "git@github.com:user/proj.git",
            "branch": "main",
            "commit": "abc",
            "repo_name": "proj",
            "project_path": "/home/user/code/proj",
        }))

        with patch("brr.state.STATE_DIR", tmp_path):
            mapping = _staging_project_map()
        assert mapping == {"my-cluster": "/home/user/code/proj"}

    def test_empty_when_no_staging(self, tmp_path):
        with patch("brr.state.STATE_DIR", tmp_path):
            assert _staging_project_map() == {}

    def test_skips_invalid_json(self, tmp_path):
        staging = tmp_path / "staging"
        cluster_dir = staging / "bad-cluster"
        cluster_dir.mkdir(parents=True)
        (cluster_dir / "repo_info.json").write_text("not json")

        with patch("brr.state.STATE_DIR", tmp_path):
            assert _staging_project_map() == {}

    def test_nested_provider_dir(self, tmp_path):
        """Nebius staging: staging/nebius/my-cluster/repo_info.json"""
        staging = tmp_path / "staging"
        cluster_dir = staging / "nebius" / "my-cluster"
        cluster_dir.mkdir(parents=True)
        (cluster_dir / "repo_info.json").write_text(json.dumps({
            "remote_url": "git@github.com:user/proj.git",
            "branch": "main",
            "commit": "abc",
            "repo_name": "proj",
            "project_path": "/home/user/code/proj",
        }))

        with patch("brr.state.STATE_DIR", tmp_path):
            mapping = _staging_project_map()
        assert "my-cluster" in mapping
