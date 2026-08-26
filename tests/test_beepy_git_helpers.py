#!/usr/bin/env python3
"""Safety tests for the Beepy contributor Git helpers.

All repositories and remotes are temporary local fixtures. No network service
is contacted and no real contributor identity is used.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER_DIR = PROJECT_ROOT / "scripts" / "dev"
STATUS = HELPER_DIR / "beepy-status"
SAVE = HELPER_DIR / "beepy-save"
SYNC = HELPER_DIR / "beepy-sync"

TEST_NAME = "Test Contributor"
TEST_EMAIL = "test-contributor@example.invalid"


def test_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    return env


ENV = test_environment()


def run(
    command: list[str | Path],
    *,
    cwd: Path,
    input_text: str | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        env=ENV,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(map(str, command))}\n"
            f"{result.stdout}"
        )
    return result


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=repo, check=check)


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "--all", "--", ".")
    git(
        repo,
        "-c",
        f"user.name={TEST_NAME}",
        "-c",
        f"user.email={TEST_EMAIL}",
        "-c",
        "user.useConfigOnly=true",
        "commit",
        "-m",
        message,
    )


def output_value(output: str, heading: str) -> str:
    lines = output.splitlines()
    index = lines.index(heading)
    return lines[index + 1]


class RepositoryFixture:
    def __init__(self, *, directory_name: str = "work") -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="beepy-helper-tests-")
        self.root = Path(self.tempdir.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / directory_name
        self.remote_work_number = 0

        self.remote.mkdir()
        git(self.remote, "init", "--bare", "--initial-branch=development")
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=development")
        (self.repo / "README.txt").write_text("initial\n", encoding="ascii")
        commit(self.repo, "Initial test commit")
        git(self.repo, "branch", "main")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "origin", "development:development", "main:main")
        git(self.repo, "branch", "--set-upstream-to=origin/development", "development")

    def close(self) -> None:
        self.tempdir.cleanup()

    def configure_identity(self, name: str = TEST_NAME, email: str = TEST_EMAIL) -> None:
        git(self.repo, "config", "--local", "user.name", name)
        git(self.repo, "config", "--local", "user.email", email)

    def helper(
        self, helper: Path, *args: str, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return run([helper, *args], cwd=self.repo, input_text=input_text)

    def head(self, revision: str = "HEAD") -> str:
        return git(self.repo, "rev-parse", revision).stdout.strip()

    def remote_head(self, branch: str = "development") -> str:
        return git(self.remote, "rev-parse", f"refs/heads/{branch}").stdout.strip()

    def add_local_commit(self, text: str = "local\n", message: str = "Local change") -> None:
        (self.repo / "README.txt").write_text(text, encoding="ascii")
        commit(self.repo, message)

    def add_remote_commit(self, text: str = "remote\n") -> str:
        self.remote_work_number += 1
        work = self.root / f"remote-work-{self.remote_work_number}"
        run(
            ["git", "clone", "--branch", "development", str(self.remote), str(work)],
            cwd=self.root,
            check=True,
        )
        (work / "README.txt").write_text(text, encoding="ascii")
        commit(work, f"Remote change {self.remote_work_number}")
        git(work, "push", "origin", "development:development")
        return git(work, "rev-parse", "HEAD").stdout.strip()

    def create_conflict(self) -> None:
        git(self.repo, "switch", "-c", "conflicting-work")
        (self.repo / "README.txt").write_text("other branch\n", encoding="ascii")
        commit(self.repo, "Other branch change")
        git(self.repo, "switch", "development")
        (self.repo / "README.txt").write_text("development branch\n", encoding="ascii")
        commit(self.repo, "Development change")
        result = git(
            self.repo,
            "-c",
            f"user.name={TEST_NAME}",
            "-c",
            f"user.email={TEST_EMAIL}",
            "merge",
            "conflicting-work",
            check=False,
        )
        if result.returncode == 0:
            raise AssertionError("test fixture did not create a conflict")


class HelperTestCase(unittest.TestCase):
    fixture: RepositoryFixture

    def setUp(self) -> None:
        self.fixture = RepositoryFixture()

    def tearDown(self) -> None:
        self.fixture.close()


class BeepyStatusTests(HelperTestCase):
    def test_clean_development(self) -> None:
        result = self.fixture.helper(STATUS)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(output_value(result.stdout, "Working tree:"), "clean")
        self.assertEqual(output_value(result.stdout, "Current branch:"), "development")
        self.assertEqual(output_value(result.stdout, "Safe to work:"), "YES")

    def test_modified_development(self) -> None:
        (self.fixture.repo / "README.txt").write_text("modified\n", encoding="ascii")
        result = self.fixture.helper(STATUS)
        self.assertEqual(output_value(result.stdout, "Working tree:"), "modified")
        self.assertEqual(output_value(result.stdout, "Unstaged changes:"), "1")

    def test_main_is_reported_unsafe(self) -> None:
        git(self.fixture.repo, "switch", "main")
        result = self.fixture.helper(STATUS)
        self.assertEqual(output_value(result.stdout, "Current branch:"), "main")
        self.assertEqual(output_value(result.stdout, "Safe to work:"), "NO")

    def test_detached_head_is_reported_unsafe(self) -> None:
        git(self.fixture.repo, "switch", "--detach")
        result = self.fixture.helper(STATUS)
        self.assertEqual(output_value(result.stdout, "Current branch:"), "detached HEAD")
        self.assertEqual(output_value(result.stdout, "Safe to work:"), "NO")

    def test_missing_identity(self) -> None:
        result = self.fixture.helper(STATUS)
        self.assertEqual(output_value(result.stdout, "Git identity:"), "missing")

    def test_configured_identity(self) -> None:
        self.fixture.configure_identity()
        result = self.fixture.helper(STATUS)
        self.assertEqual(output_value(result.stdout, "Git identity:"), "configured")
        self.assertIn(f"{TEST_NAME} <{TEST_EMAIL}>", result.stdout)

    def test_ahead_state(self) -> None:
        self.fixture.add_local_commit()
        result = self.fixture.helper(STATUS)
        self.assertEqual(output_value(result.stdout, "Ahead:"), "1")
        self.assertEqual(output_value(result.stdout, "Behind:"), "0")

    def test_behind_state_uses_last_fetched_information(self) -> None:
        self.fixture.add_remote_commit()
        git(self.fixture.repo, "fetch", "origin", "development:refs/remotes/origin/development")
        result = self.fixture.helper(STATUS)
        self.assertEqual(output_value(result.stdout, "Ahead:"), "0")
        self.assertEqual(output_value(result.stdout, "Behind:"), "1")
        self.assertIn("last fetched Git state", result.stdout)

    def test_conflict_and_operation_reporting(self) -> None:
        self.fixture.create_conflict()
        result = self.fixture.helper(STATUS)
        self.assertEqual(output_value(result.stdout, "Working tree:"), "conflicts")
        self.assertEqual(output_value(result.stdout, "Operation in progress:"), "merge")
        self.assertEqual(output_value(result.stdout, "Conflicts:"), "1")
        self.assertEqual(output_value(result.stdout, "Safe to work:"), "NO")

    def test_status_does_not_touch_index_or_head(self) -> None:
        index = self.fixture.repo / ".git" / "index"
        before_index = hashlib.sha256(index.read_bytes()).hexdigest()
        before_head = self.fixture.head()
        result = self.fixture.helper(STATUS)
        after_index = hashlib.sha256(index.read_bytes()).hexdigest()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(before_index, after_index)
        self.assertEqual(before_head, self.fixture.head())


class BeepySaveTests(HelperTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.initial_main = self.fixture.head("main")

    def assert_main_unchanged(self) -> None:
        self.assertEqual(self.initial_main, self.fixture.head("main"))

    def test_refuses_main(self) -> None:
        self.fixture.configure_identity()
        git(self.fixture.repo, "switch", "main")
        (self.fixture.repo / "README.txt").write_text("change\n", encoding="ascii")
        result = self.fixture.helper(SAVE, "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("You are on main", result.stdout)
        self.assert_main_unchanged()

    def test_refuses_detached_head(self) -> None:
        self.fixture.configure_identity()
        git(self.fixture.repo, "switch", "--detach")
        result = self.fixture.helper(SAVE, "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("detached", result.stdout)
        self.assert_main_unchanged()

    def test_refuses_conflicts_and_lists_only_paths(self) -> None:
        self.fixture.configure_identity()
        self.fixture.create_conflict()
        result = self.fixture.helper(SAVE, "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Conflicted paths:", result.stdout)
        self.assertIn("README.txt", result.stdout)
        self.assertIn("human decision", result.stdout)
        self.assert_main_unchanged()

    def test_refuses_missing_identity(self) -> None:
        (self.fixture.repo / "README.txt").write_text("change\n", encoding="ascii")
        result = self.fixture.helper(SAVE, "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("user.name and user.email", result.stdout)
        self.assert_main_unchanged()

    def test_rejects_archival_jerry_identity(self) -> None:
        self.fixture.configure_identity("Jerry Sandy", "beepy-history@invalid.example")
        (self.fixture.repo / "README.txt").write_text("change\n", encoding="ascii")
        result = self.fixture.helper(SAVE, "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reserved", result.stdout)
        self.assert_main_unchanged()

    def test_rejects_migration_identity(self) -> None:
        self.fixture.configure_identity(
            "Beepy Repository Migration", "beepy-migration@invalid.example"
        )
        (self.fixture.repo / "README.txt").write_text("change\n", encoding="ascii")
        result = self.fixture.helper(SAVE, "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reserved", result.stdout)
        self.assert_main_unchanged()

    def test_blocks_sensitive_paths_without_reading_content(self) -> None:
        self.fixture.configure_identity()
        (self.fixture.repo / "client-credentials.txt").write_text(
            "fixture-not-a-secret\n", encoding="ascii"
        )
        result = self.fixture.helper(SAVE, "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("client-credentials.txt", result.stdout)
        self.assertNotIn("fixture-not-a-secret", result.stdout)
        self.assertEqual(git(self.fixture.repo, "diff", "--cached", "--name-only").stdout, "")
        self.assert_main_unchanged()

    def test_env_example_is_allowed(self) -> None:
        self.fixture.configure_identity()
        (self.fixture.repo / ".env.example").write_text("EXAMPLE=value\n", encoding="ascii")
        result = self.fixture.helper(SAVE, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(".env.example", result.stdout)
        self.assertNotIn("Blocked paths:", result.stdout)
        self.assert_main_unchanged()

    def test_dry_run_makes_zero_changes(self) -> None:
        self.fixture.configure_identity()
        (self.fixture.repo / "README.txt").write_text("dry run\n", encoding="ascii")
        index = self.fixture.repo / ".git" / "index"
        before_head = self.fixture.head()
        before_status = git(self.fixture.repo, "status", "--short").stdout
        before_index = hashlib.sha256(index.read_bytes()).hexdigest()
        before_remote = self.fixture.remote_head()
        result = self.fixture.helper(SAVE, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Dry-run complete", result.stdout)
        self.assertEqual(before_index, hashlib.sha256(index.read_bytes()).hexdigest())
        self.assertEqual(before_head, self.fixture.head())
        self.assertEqual(before_status, git(self.fixture.repo, "status", "--short").stdout)
        self.assertEqual(before_remote, self.fixture.remote_head())
        self.assert_main_unchanged()

    def test_reviewed_commit_succeeds_and_push_is_separate(self) -> None:
        self.fixture.configure_identity()
        (self.fixture.repo / "README.txt").write_text("checkpoint\n", encoding="ascii")
        before_remote = self.fixture.remote_head()
        result = self.fixture.helper(
            SAVE, input_text="y\nSave reviewed checkpoint\ny\nn\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Your checkpoint is now saved locally", result.stdout)
        self.assertIn("Not pushed", result.stdout)
        self.assertEqual(
            git(self.fixture.repo, "show", "-s", "--format=%s", "HEAD").stdout.strip(),
            "Save reviewed checkpoint",
        )
        self.assertEqual(before_remote, self.fixture.remote_head())
        self.assert_main_unchanged()

    def test_commit_message_is_not_shell_evaluated(self) -> None:
        self.fixture.configure_identity()
        (self.fixture.repo / "README.txt").write_text("safe message\n", encoding="ascii")
        message = "Fix $(touch SHOULD_NOT_EXIST); echo no"
        result = self.fixture.helper(
            SAVE, input_text=f"y\n{message}\ny\nn\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.fixture.repo / "SHOULD_NOT_EXIST").exists())
        self.assertEqual(
            git(self.fixture.repo, "show", "-s", "--format=%s", "HEAD").stdout.strip(),
            message,
        )
        self.assert_main_unchanged()

    def test_push_rejection_does_not_force_or_rewrite(self) -> None:
        self.fixture.configure_identity()
        remote_head = self.fixture.add_remote_commit("remote competing\n")
        (self.fixture.repo / "local-file.txt").write_text("local\n", encoding="ascii")
        result = self.fixture.helper(
            SAVE, input_text="y\nLocal competing checkpoint\ny\ny\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("remote branch changed", result.stdout)
        self.assertIn("Nothing was overwritten", result.stdout)
        self.assertEqual(remote_head, self.fixture.remote_head())
        self.assertNotEqual(self.fixture.head(), remote_head)
        self.assert_main_unchanged()

    def test_optional_push_succeeds_only_after_separate_confirmation(self) -> None:
        self.fixture.configure_identity()
        (self.fixture.repo / "published-file.txt").write_text("publish\n", encoding="ascii")
        result = self.fixture.helper(
            SAVE, input_text="y\nPublish reviewed checkpoint\ny\ny\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Push complete", result.stdout)
        self.assertEqual(self.fixture.head(), self.fixture.remote_head())
        self.assert_main_unchanged()

    def test_paths_with_spaces_are_handled_safely(self) -> None:
        self.fixture.configure_identity()
        spaced = self.fixture.repo / "directory with spaces" / "new file.txt"
        spaced.parent.mkdir()
        spaced.write_text("new\n", encoding="ascii")
        result = self.fixture.helper(SAVE, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("directory with spaces", result.stdout)
        self.assertEqual(git(self.fixture.repo, "diff", "--cached", "--name-only").stdout, "")
        self.assert_main_unchanged()


class BeepySyncTests(HelperTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.initial_main = self.fixture.head("main")

    def assert_main_unchanged(self) -> None:
        self.assertEqual(self.initial_main, self.fixture.head("main"))

    def test_refuses_main(self) -> None:
        git(self.fixture.repo, "switch", "main")
        result = self.fixture.helper(SYNC)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("You are on main", result.stdout)
        self.assert_main_unchanged()

    def test_refuses_detached_head(self) -> None:
        git(self.fixture.repo, "switch", "--detach")
        result = self.fixture.helper(SYNC)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("detached", result.stdout)
        self.assert_main_unchanged()

    def test_equal_state(self) -> None:
        result = self.fixture.helper(SYNC)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("LOCAL == REMOTE", result.stdout)
        self.assert_main_unchanged()

    def test_behind_only_fast_forward(self) -> None:
        remote_head = self.fixture.add_remote_commit()
        result = self.fixture.helper(SYNC, input_text="y\n")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("LOCAL BEHIND REMOTE ONLY", result.stdout)
        self.assertIn("Fast-forward complete", result.stdout)
        self.assertEqual(remote_head, self.fixture.head())
        self.assert_main_unchanged()

    def test_ahead_only_reports_without_modification(self) -> None:
        self.fixture.add_local_commit()
        before = self.fixture.head()
        result = self.fixture.helper(SYNC)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("LOCAL AHEAD REMOTE ONLY", result.stdout)
        self.assertEqual(before, self.fixture.head())
        self.assert_main_unchanged()

    def test_diverged_history_stops(self) -> None:
        self.fixture.add_local_commit("local competing\n")
        local_head = self.fixture.head()
        remote_head = self.fixture.add_remote_commit("remote competing\n")
        result = self.fixture.helper(SYNC)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LOCAL AND REMOTE DIVERGED", result.stdout)
        self.assertIn("will not guess", result.stdout)
        self.assertEqual(local_head, self.fixture.head())
        self.assertEqual(remote_head, self.fixture.remote_head())
        self.assert_main_unchanged()

    def test_dirty_worktree_update_stops_without_stash(self) -> None:
        self.fixture.add_remote_commit()
        (self.fixture.repo / "local notes.txt").write_text("unsaved\n", encoding="ascii")
        before_head = self.fixture.head()
        result = self.fixture.helper(SYNC)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("working tree has unsaved changes", result.stdout)
        self.assertIn("no changes were stashed", result.stdout.lower())
        self.assertEqual(before_head, self.fixture.head())
        self.assertTrue((self.fixture.repo / "local notes.txt").exists())
        self.assertEqual(git(self.fixture.repo, "stash", "list").stdout, "")
        self.assert_main_unchanged()

    def test_conflict_is_refused_without_resolution(self) -> None:
        self.fixture.create_conflict()
        before = (self.fixture.repo / "README.txt").read_text(encoding="ascii")
        result = self.fixture.helper(SYNC)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("human decision", result.stdout)
        self.assertEqual(before, (self.fixture.repo / "README.txt").read_text(encoding="ascii"))
        self.assert_main_unchanged()


class GeneralSafetyTests(unittest.TestCase):
    def test_help_output_is_ascii_and_documents_safety(self) -> None:
        for helper in (STATUS, SAVE, SYNC):
            result = run([helper, "--help"], cwd=PROJECT_ROOT)
            self.assertEqual(result.returncode, 0, result.stdout)
            result.stdout.encode("ascii")
        save_help = run([SAVE, "--help"], cwd=PROJECT_ROOT).stdout
        self.assertIn("never force pushes", save_help)
        self.assertIn("modifies main", save_help)

    def test_shell_sources_have_no_eval_or_forbidden_git_operations(self) -> None:
        forbidden = (
            "eval ",
            "git rebase",
            "git reset --hard",
            "git clean",
            "git commit --amend",
            "git push --force",
            "git push --force-with-lease",
            "git stash",
        )
        for path in HELPER_DIR.rglob("*"):
            if not path.is_file():
                continue
            source = path.read_text(encoding="ascii")
            for text in forbidden:
                self.assertNotIn(text, source, f"{text!r} found in {path}")

    def test_failures_are_nonzero(self) -> None:
        with tempfile.TemporaryDirectory(prefix="beepy-helper-not-repo-") as directory:
            cwd = Path(directory)
            for helper in (STATUS, SAVE, SYNC):
                result = run([helper], cwd=cwd)
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_user_local_style_symlink_resolves_shared_library(self) -> None:
        fixture = RepositoryFixture()
        try:
            bin_dir = fixture.root / "user local bin"
            bin_dir.mkdir()
            linked_status = bin_dir / "beepy-status"
            linked_status.symlink_to(STATUS)
            result = run([linked_status], cwd=fixture.repo)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("Expected working branch:\ndevelopment", result.stdout)
        finally:
            fixture.close()

    def test_remote_with_embedded_credentials_is_rejected_without_display(self) -> None:
        fixture = RepositoryFixture()
        try:
            git(fixture.repo, "remote", "set-url", "origin", "https://secret-value@github.com/example/beepy.git")
            result = fixture.helper(SYNC)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not recognized", result.stdout)
            self.assertNotIn("secret-value", result.stdout)
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
