"""Full pipeline integration test — exercises the LangGraph state machine end-to-end.

Mocks the Docker sandbox and LLM calls so the test runs without external deps,
while verifying the state machine transitions through every node correctly.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import pytest
from unittest.mock import patch, MagicMock


# ── Helpers ──────────────────────────────────────────────────────────────────

MOCK_PATCH_CONTENT = (
    '"""Auth module."""\n'
    'def validate_token(token):\n'
    '    if len(token.strip()) < 10:\n'
    '        raise ValueError("Token must be at least 10 characters long")\n'
    '    return {"valid": True, "token": token.strip()}\n'
)

MOCK_DIFF = (
    "--- a/src/auth.py\n+++ b/src/auth.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-    if len(token.strip()) < 80:\n"
    "+    if len(token.strip()) < 10:\n"
)


def _sandbox_pass(*args, **kwargs):
    """Simulates a passing sandbox run."""
    return {
        "passed": True,
        "output": "tests/test_auth.py::test_valid_token PASSED\n1 passed in 0.1s",
        "exit_code": 0,
    }


def _sandbox_baseline(*args, **kwargs):
    """Simulates a baseline capture with one failing test."""
    return {
        "passed": False,
        "output": (
            "tests/test_auth.py::test_valid_token FAILED\n"
            "tests/test_auth.py::test_short_token_raises_value_error PASSED\n"
            "1 failed, 1 passed in 0.2s"
        ),
        "exit_code": 1,
    }


# ── Tests ────────────────────────────────────────────────────────────────────

@patch("agents.regression_test_generator.call_llm", return_value="def test_regression(): assert True")
@patch("agents.git_bisect_agent.run_bisect", return_value={"bisect_intent_report": "Commit abc1234 identified as bad commit."})
@patch("agents.analyst_agent.call_llm", return_value='{"failing_test": "tests/test_auth.py::test_valid_token", "failing_file": "src/auth.py", "failing_line": 12, "root_cause_summary": "Token length threshold too high", "suspected_function": "validate_token"}')
@patch("agents.pr_agent.get_file_content", return_value='def validate_token(token):\n    if len(token) < 80:\n        raise ValueError("Token must be at least 10 characters long")\n')
@patch("agents.patch_generator.call_llm", return_value=MOCK_PATCH_CONTENT)
@patch("agents.validator_agent.run_sandbox")
@patch("agents.sandbox_runner.run_sandbox")
@patch("agents.orchestrator.create_pr", return_value="https://github.com/test/repo/pull/99")
@patch("agents.chroma_memory.init_memory", return_value=MagicMock())
@patch("agents.chroma_memory.store_patch")
@patch("agents.chroma_memory.query_memory", return_value=None)
def test_pipeline_happy_path(
    mock_chroma_query, mock_store, mock_init_mem,
    mock_create_pr, mock_sandbox_runner, mock_val_sandbox, mock_llm, mock_get_content, mock_analyst_llm, mock_bisect, mock_regtest_llm
):
    """Full pipeline: IDLE → ANALYZING → BISECTING → PATCHING → VALIDATING → PR → DONE."""
    # First sandbox call = baseline capture (failing), subsequent = validation (passing)
    mock_val_sandbox.side_effect = [_sandbox_baseline(), _sandbox_pass()]
    mock_sandbox_runner.side_effect = [_sandbox_baseline(), _sandbox_pass()]

    from agents.orchestrator import run_pipeline

    event = {
        "execution_mode": "local",
        "repo_full_name": "test-org/test-repo",
        "commit_sha": "abc1234567890",
        "failure_log": (
            "FAILED tests/test_auth.py::test_valid_token - ValueError: "
            "Token must be at least 10 characters long\n"
            "File \"src/auth.py\", line 12, in validate_token\n"
            "    if len(cleaned_token) < 80:"
        ),
        "branch": "main",
        "source": "webhook",
    }

    with patch("agents.orchestrator.run_bisect", side_effect=lambda state: state), patch.dict(os.environ, {"ARCANE_DRY_RUN": "0"}):
        result = asyncio.run(run_pipeline(event))

    # Pipeline should reach DONE with a PR URL
    assert result.get("pr_url"), f"Expected pr_url, got: {result}"
    assert result.get("tests_passed") is True
    assert result.get("confidence_score", 0) >= 60
    assert "test-org/test-repo" in result.get("repo_full_name", "")
    assert mock_val_sandbox.call_count >= 1


@patch("agents.analyst_agent.call_llm", return_value='{"failing_test": "tests/test_auth.py::test_valid_token", "failing_file": "src/auth.py", "failing_line": 12, "root_cause_summary": "Token length threshold too high", "suspected_function": "validate_token"}')
@patch("agents.pr_agent.get_file_content", return_value='def validate_token(token):\n    if len(token) < 80:\n        raise ValueError("Token must be at least 10 characters long")\n')
@patch("agents.patch_generator.call_llm", return_value="Error: all models failed")
@patch("agents.validator_agent.run_sandbox")
@patch("agents.sandbox_runner.run_sandbox")
@patch("agents.chroma_memory.query_memory", return_value=None)
def test_pipeline_escalation_path(mock_chroma_query, mock_sandbox_runner, mock_val_sandbox, mock_llm, mock_get_content, mock_analyst_llm):
    """Pipeline escalates after MAX_RETRIES failed patch attempts."""
    mock_val_sandbox.return_value = {
        "passed": False,
        "output": "FAILED tests/test_auth.py::test_valid_token",
        "exit_code": 1,
    }
    mock_sandbox_runner.return_value = {
        "passed": False,
        "output": "FAILED tests/test_auth.py::test_valid_token",
        "exit_code": 1,
    }

    from agents.orchestrator import run_pipeline

    event = {
        "execution_mode": "local",
        "repo_full_name": "test-org/test-repo",
        "commit_sha": "deadbeef1234",
        "failure_log": "FAILED tests/test_auth.py::test_valid_token",
        "branch": "main",
        "source": "webhook",
    }

    with patch("agents.orchestrator.run_bisect", side_effect=lambda state: state):
        result = asyncio.run(run_pipeline(event))

    # Pipeline should escalate — no PR created
    assert result.get("error") is not None
    assert "did not return corrected source" in result["error"]
    assert result.get("retry_count", 0) >= 3


@patch("agents.regression_test_generator.call_llm", return_value="def test_regression(): assert True")
@patch("agents.git_bisect_agent.run_hosted_commit_inspection", return_value={"bisect_intent_report": "[HOSTED COMMIT INSPECTION — UNVERIFIED]"})
@patch("agents.analyst_agent.call_llm", return_value='{"failing_test": "tests/test_auth.py::test_valid_token", "failing_file": "src/auth.py", "failing_line": 12, "root_cause_summary": "Token length threshold too high", "suspected_function": "validate_token"}')
@patch("agents.pr_agent.get_file_content", return_value='def validate_token(token):\n    if len(token) < 80:\n        raise ValueError("Token must be at least 10 characters long")\n')
@patch("agents.patch_generator.call_llm", return_value=MOCK_PATCH_CONTENT)
@patch("agents.orchestrator.create_pr", return_value="https://github.com/test/repo/pull/100-draft")
@patch("agents.chroma_memory.init_memory", return_value=MagicMock())
@patch("agents.chroma_memory.store_patch")
@patch("agents.chroma_memory.query_memory", return_value=None)
def test_pipeline_hosted_happy_path(
    mock_chroma_query, mock_store, mock_init_mem,
    mock_create_pr, mock_llm, mock_get_content, mock_analyst_llm, mock_bisect, mock_regtest_llm
):
    """Hosted flow: IDLE → ANALYZING → BISECTING (inspection) → PATCHING → VALIDATING (static) → DRAFT PR → DONE."""
    from agents.orchestrator import run_pipeline

    event = {
        "execution_mode": "hosted",
        "repo_full_name": "test-org/test-repo",
        "commit_sha": "abc1234567890",
        "failure_log": (
            "FAILED tests/test_auth.py::test_valid_token - ValueError: "
            "Token must be at least 10 characters long\n"
        ),
        "branch": "main",
        "source": "webhook",
    }

    context = {"changes": {"src/auth.py": "changed threshold"}, "traces": [], "tests": {},
               "candidates": ["src/auth.py"], "failed_tests": ["tests/test_auth.py::test_valid_token"]}
    with patch("agents.github_diagnostics.historical_context", return_value=context), patch.dict(os.environ, {"ARCANE_DRY_RUN": "0"}):
        result = asyncio.run(run_pipeline(event))

    assert result.get("pr_url") == "https://github.com/test/repo/pull/100-draft"
    assert result.get("execution_mode") == "hosted"
    assert result.get("is_draft_pr") is True
    assert result.get("validation_status") == "SKIPPED"
    assert result.get("test_verification") == "NOT_VERIFIED"
    assert result.get("tests_passed") is False
    assert result.get("auto_approved") is False


def test_pipeline_graph_structure():
    """Verify the LangGraph state machine has the correct nodes and edges."""
    from agents.orchestrator import graph

    drawable = graph.get_graph()
    node_ids = set(drawable.nodes.keys())

    expected_nodes = {
        "idle", "analyzing", "bisecting", "patching",
        "propagating", "conflict_checking", "validating",
        "generating_test", "creating_pr", "done", "escalated",
    }
    for n in expected_nodes:
        assert n in node_ids, f"Missing node: {n}"


def test_pipeline_state_schema():
    """Verify the ArcaneState TypedDict has all critical fields."""
    from agents.orchestrator import ArcaneState
    import typing

    hints = typing.get_type_hints(ArcaneState)
    required_fields = [
        "event", "repo_full_name", "commit_sha", "failure_log",
        "failing_test", "failing_file", "patch_diff", "tests_passed",
        "confidence_score", "pr_url", "retry_count",
    ]
    for field in required_fields:
        assert field in hints, f"Missing field in ArcaneState: {field}"
