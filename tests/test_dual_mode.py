"""Tests for ARCANE Dual-Mode Execution Architecture (Local Docker + Cloud Hosted Without Docker).

Covers all 12 key scenarios:
1. Local mode with Docker available.
2. Local mode with Docker unavailable (raises RuntimeError).
3. Hosted mode configuration (never calls Docker).
4. Auto mode detection (resolves local or hosted without crashing).
5. Hosted commit inspection via GitHub API.
6. Real patch generation preservation in hosted mode.
7. Static syntax/AST and unified diff validation in hosted mode.
8. Validation status model (PASSED, FAILED, SKIPPED, ERROR).
9. LangGraph routing prevention of infinite retry loop for skipped tests.
10. Draft PR creation with unverified warning banner in hosted mode.
11. Verified PR creation in local mode.
12. Dry-run compatibility across both execution modes.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ast
import pytest
from unittest.mock import patch, MagicMock

from agents.execution_mode import (
    is_docker_available,
    get_execution_mode,
    reset_docker_cache,
    is_hosted_mode,
)
from agents.git_bisect_agent import run_bisect, run_hosted_commit_inspection
from agents.validator_agent import validate, validate_hosted_static, capture_baseline
from agents.orchestrator import after_validating, creating_pr_node
from agents.pr_agent import create_pr


class TestExecutionModeDetection:
    """Tests 1-4: Environment variable resolution and Docker availability checks."""

    def setup_method(self):
        reset_docker_cache()

    def teardown_method(self):
        reset_docker_cache()

    @patch("agents.execution_mode.subprocess.run")
    def test_local_mode_with_docker_available(self, mock_subproc):
        """Scenario 1: local mode returns 'local' when Docker daemon is responsive."""
        mock_subproc.return_value = MagicMock(returncode=0)
        with patch.dict(os.environ, {"ARCANE_EXECUTION_MODE": "local"}):
            mode = get_execution_mode(force_probe=True)
            assert mode == "local"
            assert is_docker_available() is True

    @patch("agents.execution_mode.subprocess.run")
    def test_local_mode_with_docker_unavailable_raises_error(self, mock_subproc):
        """Scenario 2: local mode raises RuntimeError when Docker daemon is down or missing."""
        mock_subproc.return_value = MagicMock(returncode=1)
        with patch.dict(os.environ, {"ARCANE_EXECUTION_MODE": "local"}):
            with pytest.raises(RuntimeError) as exc_info:
                get_execution_mode(force_probe=True)
            assert "ARCANE_EXECUTION_MODE is set to 'local', but the Docker daemon is not" in str(exc_info.value)

    @patch("agents.execution_mode.subprocess.run")
    def test_hosted_mode_never_calls_docker(self, mock_subproc):
        """Scenario 3: hosted mode always returns 'hosted' and bypasses Docker probing."""
        with patch.dict(os.environ, {"ARCANE_EXECUTION_MODE": "hosted"}):
            mode = get_execution_mode()
            assert mode == "hosted"
            assert is_hosted_mode() is True
            # subprocess.run must NOT be invoked when mode is explicitly hosted
            mock_subproc.assert_not_called()

    @patch("agents.execution_mode.subprocess.run")
    def test_auto_mode_resolves_cleanly(self, mock_subproc):
        """Scenario 4: auto mode selects local when Docker is up, hosted when Docker is down."""
        # Case 4a: Docker available
        mock_subproc.return_value = MagicMock(returncode=0)
        with patch.dict(os.environ, {"ARCANE_EXECUTION_MODE": "auto"}):
            reset_docker_cache()
            assert get_execution_mode(force_probe=True) == "local"

        # Case 4b: Docker unavailable (FileNotFoundError)
        mock_subproc.side_effect = FileNotFoundError("No such file or directory: 'docker'")
        with patch.dict(os.environ, {"ARCANE_EXECUTION_MODE": "auto"}):
            reset_docker_cache()
            assert get_execution_mode(force_probe=True) == "hosted"


class TestHostedGitBisectInspection:
    """Tests 5-6: Hosted Git commit inspection without Docker."""

    @patch("agents.git_bisect_agent.run_hosted_commit_inspection")
    def test_run_bisect_delegates_to_hosted_in_hosted_mode(self, mock_hosted):
        """Scenario 5: run_bisect routes to hosted inspection when in hosted mode."""
        mock_hosted.return_value = {"bisect_intent_report": "[HOSTED COMMIT INSPECTION — UNVERIFIED]"}
        state = {
            "execution_mode": "hosted",
            "repo_full_name": "owner/repo",
            "commit_sha": "abc1234",
            "failing_file": "api/auth.py",
        }
        res = run_bisect(state)
        mock_hosted.assert_called_once()
        assert "[HOSTED COMMIT INSPECTION" in res["bisect_intent_report"]

    @patch("github.Github")
    def test_hosted_commit_inspection_identifies_suspected_commit(self, mock_gh_cls):
        """Scenario 6: GitHub API inspected commits identify culprit file modification."""
        mock_file = MagicMock(filename="api/auth.py", patch="@@ -1 +1 @@\n-old\n+new")
        mock_commit = MagicMock(
            sha="abcdef1234567890abcdef1234567890abcdef12",
            commit=MagicMock(
                author=MagicMock(name="Jane Dev"),
                message="feat: modify authentication handler",
            ),
            files=[mock_file],
        )
        mock_repo = MagicMock()
        mock_repo.get_commits.return_value = [mock_commit]
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_gh_cls.return_value = mock_gh

        state = {
            "repo_full_name": "owner/repo",
            "commit_sha": "abcdef1234567890abcdef1234567890abcdef12",
            "failing_file": "api/auth.py",
            "failing_test": "test_auth.py::test_login",
        }
        res = run_hosted_commit_inspection(state)

        assert res["bisect_mode"] == "hosted_inspection"
        assert res["bisect_confidence"] == "uncertain"
        assert "[HOSTED COMMIT INSPECTION — UNVERIFIED]" in res["bisect_intent_report"]
        assert "Suspected Breaking Commit: abcdef1" in res["bisect_intent_report"]
        assert "Jane Dev" in res["bisect_intent_report"]


class TestDualModeValidation:
    """Tests 7-9: Safe static validation and LangGraph routing."""

    def test_hosted_static_validation_success(self):
        """Scenario 7a: Valid Python AST and unified diff produces SKIPPED validation status."""
        valid_python = "def calculate_total(a, b):\n    return a + b\n"
        valid_diff = "--- a/math_ops.py\n+++ b/math_ops.py\n@@ -1,2 +1,2 @@\n-    return a - b\n+    return a + b\n"

        state = {
            "execution_mode": "hosted",
            "failing_file": "math_ops.py",
            "patched_content": valid_python,
            "patch_diff": valid_diff,
        }
        res = validate_hosted_static(state)

        assert res["validation_status"] == "SKIPPED"
        assert res["test_verification"] == "NOT_VERIFIED"
        assert res["tests_passed"] is False  # Must NEVER be True for skipped tests
        assert res["auto_approved"] is False  # Must NEVER auto-approve unverified tests
        assert res["confidence_score"] == 50.0
        assert "Docker sandbox skipped" in res["validator_summary"]

    def test_hosted_static_validation_syntax_error(self):
        """Scenario 7b: Invalid Python syntax in patched file sets validation_status to ERROR."""
        invalid_python = "def calculate_total(a, b:\n    return a + b\n"  # Missing closing paren
        valid_diff = "--- a/math_ops.py\n+++ b/math_ops.py\n@@ -1 +1 @@\n"

        state = {
            "execution_mode": "hosted",
            "failing_file": "math_ops.py",
            "patched_content": invalid_python,
            "patch_diff": valid_diff,
            "retry_count": 0,
        }
        res = validate_hosted_static(state)

        assert res["validation_status"] == "ERROR"
        assert res["test_verification"] == "NOT_VERIFIED"
        assert res["tests_passed"] is False
        assert "Python syntax error" in res["validator_summary"]

    @patch("agents.validator_agent.run_sandbox")
    def test_capture_baseline_skips_docker_in_hosted_mode(self, mock_sandbox):
        """Scenario 8: capture_baseline does not invoke Docker container in hosted mode."""
        state = {
            "execution_mode": "hosted",
            "repo_full_name": "owner/repo",
            "commit_sha": "abc1234",
        }
        res = capture_baseline(state)
        mock_sandbox.assert_not_called()
        assert res["baseline_passing_tests"] == []
        assert res["baseline_failing_tests"] == []

    def test_after_validating_routing_for_skipped_status(self):
        """Scenario 9: LangGraph routing moves SKIPPED to generating_test (no infinite retry loop)."""
        # When validation is SKIPPED (hosted mode, static checks passed)
        state_skipped = {
            "validation_status": "SKIPPED",
            "tests_passed": False,
            "retry_count": 0,
            "confidence_score": 50.0,
        }
        next_step = after_validating(state_skipped)
        assert next_step == "generating_test"

        # When validation status is ERROR (e.g. syntax error), loop back to patching if retries remain
        state_error = {
            "validation_status": "ERROR",
            "tests_passed": False,
            "retry_count": 1,
            "confidence_score": 25.0,
        }
        assert after_validating(state_error) == "patching"

        # Local mode: tests_passed=True routes to generating_test
        state_passed = {
            "validation_status": "PASSED",
            "tests_passed": True,
            "retry_count": 0,
            "confidence_score": 90.0,
        }
        assert after_validating(state_passed) == "generating_test"

        # Local mode: tests_passed=False and retries exhausted routes to escalated
        state_exhausted = {
            "validation_status": "FAILED",
            "tests_passed": False,
            "retry_count": 3,
            "confidence_score": 30.0,
        }
        assert after_validating(state_exhausted) == "escalated"


class TestDualModePullRequests:
    """Tests 10-12: Pull request behaviors (Draft PR, Verified PR, Dry Run)."""

    @patch("agents.pr_agent.Github")
    def test_create_pr_hosted_mode_creates_draft_with_warning(self, mock_gh_cls):
        """Scenario 10: Hosted mode opens a Draft PR with unverified testing warning."""
        mock_repo = MagicMock()
        mock_pull = MagicMock(html_url="https://github.com/owner/repo/pull/101")
        mock_repo.create_pull.return_value = mock_pull
        mock_repo.get_git_ref.return_value = MagicMock(object=MagicMock(sha="base123"))

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_gh_cls.return_value = mock_gh

        mock_repo.get_contents.return_value.decoded_content = b"1\n"
        mock_repo.get_contents.return_value.sha = "source-blob"
        mock_repo.get_git_commit.return_value.tree.tree = [MagicMock(path="f.py", mode="100644", sha="source-blob")]
        mock_repo.create_git_tree.return_value.sha = "tree-sha"
        state = {
            "patched_content": "2\n",
            "execution_mode": "hosted",
            "validation_status": "SKIPPED",
            "repo_full_name": "owner/repo",
            "commit_sha": "abc1234567",
            "patch_diff": "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-1\n+2",
            "failing_file": "f.py",
            "failing_test": "test_f",
            "root_cause_summary": "Typo in constant",
            "confidence_score": 50.0,
            "tests_passed": False,
        }

        with patch.dict(os.environ, {"GITHUB_PAT": "ghp_fake_token_for_testing"}):
            url = create_pr(state)

        assert url == "https://github.com/owner/repo/pull/101"
        assert state["is_draft_pr"] is True

        # Verify draft=True was passed to PyGithub
        _, kwargs = mock_repo.create_pull.call_args
        assert kwargs.get("draft") is True
        assert "[DRAFT]" in kwargs.get("title")
        assert "UNVERIFIED DRAFT PULL REQUEST" in kwargs.get("body")
        assert "Docker Sandbox Execution:** SKIPPED" in kwargs.get("body")

    @patch("agents.pr_agent.Github")
    def test_create_pr_local_mode_creates_verified_pr(self, mock_gh_cls):
        """Scenario 11: Local mode with verified tests opens standard non-draft PR."""
        mock_repo = MagicMock()
        mock_pull = MagicMock(html_url="https://github.com/owner/repo/pull/102")
        mock_repo.create_pull.return_value = mock_pull
        mock_repo.get_git_ref.return_value = MagicMock(object=MagicMock(sha="base123"))

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_gh_cls.return_value = mock_gh

        state = {
            "execution_mode": "local",
            "validation_status": "PASSED",
            "repo_full_name": "owner/repo",
            "commit_sha": "abc1234567",
            "patch_diff": "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-1\n+2",
            "failing_file": "f.py",
            "failing_test": "test_f",
            "root_cause_summary": "Typo in constant",
            "confidence_score": 95.0,
            "tests_passed": True,
        }

        with patch.dict(os.environ, {"GITHUB_PAT": "ghp_fake_token_for_testing"}):
            url = create_pr(state)

        assert url == "https://github.com/owner/repo/pull/102"
        assert state["is_draft_pr"] is False

        _, kwargs = mock_repo.create_pull.call_args
        assert kwargs.get("draft") is False
        assert "[DRAFT]" not in kwargs.get("title")

    def test_dry_run_mode_compatibility(self):
        """Scenario 12: ARCANE_DRY_RUN=1 returns no PR URL without making GitHub calls."""
        # 12a: Hosted dry run
        state_hosted = {
            "execution_mode": "hosted",
            "validation_status": "SKIPPED",
            "repo_full_name": "owner/repo",
            "commit_sha": "1234567890",
        }
        with patch.dict(os.environ, {"ARCANE_DRY_RUN": "1"}):
            res_hosted = creating_pr_node(state_hosted)
            assert res_hosted["is_draft_pr"] is True
            assert res_hosted["pr_url"] is None

        # 12b: Local dry run
        state_local = {
            "execution_mode": "local",
            "validation_status": "PASSED",
            "repo_full_name": "owner/repo",
            "commit_sha": "1234567890",
        }
        with patch.dict(os.environ, {"ARCANE_DRY_RUN": "1"}):
            res_local = creating_pr_node(state_local)
            assert res_local["is_draft_pr"] is False
            assert res_local["pr_url"] is None
