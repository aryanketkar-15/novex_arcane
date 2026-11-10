import sys
import os
import re
import ast
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.sandbox_runner import run_sandbox
from agents.execution_mode import get_execution_mode


def validate(state: Dict[str, Any]) -> Dict[str, Any]:
    """Validator Agent:
    - In local mode: applies patch in Docker sandbox and runs pytest.
    - In hosted mode: runs safe static syntax/AST and diff checks without Docker,
      marking sandbox execution as SKIPPED and tests as NOT_VERIFIED.
    """
    mode = state.get("execution_mode") or get_execution_mode()
    state["execution_mode"] = mode

    if mode == "hosted":
        logger.info("[Validator] Running in hosted mode (safe static checks, no Docker)")
        return validate_hosted_static(state)

    logger.info("[Validator] Running in local mode (Docker sandbox pytest)")
    return validate_local_docker(state)


def validate_hosted_static(state: Dict[str, Any]) -> Dict[str, Any]:
    """Performs safe static verification on the candidate patch without Docker.

    Never executes repository code on the host.
    Sets validation_status to SKIPPED, test_verification to NOT_VERIFIED,
    and tests_passed to False (never simulates passing tests).
    """
    patch_diff = state.get("patch_diff", "")
    patched_content = state.get("patched_content", "")
    failing_file = state.get("failing_file", "")

    state["validation_status"] = "SKIPPED"
    state["test_verification"] = "NOT_VERIFIED"
    state["tests_passed"] = False
    state["auto_approved"] = False
    state["cascade_failure"] = False
    state["timeout"] = False
    state["validator_exit_code"] = None

    if not patched_content.strip() or not patch_diff or not patch_diff.strip():
        state["validation_status"] = "ERROR"
        state["confidence_score"] = 0.0
        state["validator_summary"] = "Static check failed: No patch diff was generated."
        return state

    # 1. Check patch format: unified diff structure
    if "@@" not in patch_diff or not (patch_diff.startswith("---") or "--- a/" in patch_diff):
        state["validation_status"] = "ERROR"
        state["confidence_score"] = 20.0
        state["validator_summary"] = "Static check warning: Patch diff does not follow standard unified diff structure."
        return state

    # 2. Check Python AST syntax if it's a python file and patched_content is available
    if failing_file.endswith(".py") and patched_content:
        try:
            ast.parse(patched_content, filename=failing_file)
        except SyntaxError as e:
            state["validation_status"] = "ERROR"
            state["confidence_score"] = 25.0
            state["validator_summary"] = f"Static check failed: Python syntax error in patched file: {e}."
            state["retry_count"] = state.get("retry_count", 0) + 1
            return state

    lines = patch_diff.splitlines()
    if (lines[:2] != [f"--- a/{failing_file}", f"+++ b/{failing_file}"]
            or not any(line.startswith(("+", "-")) for line in lines[2:])):
        state["validation_status"] = "ERROR"
        state["validator_summary"] = "Diff has no source edit for the diagnosed file."
        return state

    # Static checks passed successfully
    state["validation_status"] = "SKIPPED"
    state["test_verification"] = "NOT_VERIFIED"
    state["confidence_score"] = 50.0  # Moderate unverified confidence, below 85% auto-approval
    state["validator_summary"] = (
        "Docker sandbox skipped in hosted cloud environment. "
        "Tests NOT verified (no container execution). "
        "Static checks (AST syntax & diff structure): PASSED."
    )
    return state


def validate_local_docker(state: Dict[str, Any]) -> Dict[str, Any]:
    """Runs tests inside an isolated Docker container."""
    repo_full_name = state.get("repo_full_name", "")
    commit_sha = state.get("commit_sha", "")
    patch_diff = state.get("patch_diff")

    token = os.getenv("GITHUB_PAT")
    if token and repo_full_name:
        repo_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
    else:
        repo_url = f"https://github.com/{repo_full_name}" if repo_full_name else ""

    sandbox_result = run_sandbox(repo_url, commit_sha, patch_diff)

    exit_code = sandbox_result.get("exit_code", 1)
    output = sandbox_result.get("output", "")

    current_retry = state.get("retry_count", 0)

    post_patch_passing = set()
    post_patch_failing = set()
    pattern = r"(\S+)[ \t]+(PASSED|FAILED)"
    for match in re.finditer(pattern, output):
        test_path = match.group(1)
        status_match = match.group(2)
        if status_match == "PASSED":
            post_patch_passing.add(test_path)
        elif status_match == "FAILED":
            post_patch_failing.add(test_path)

    baseline_failing_tests = set(state.get("baseline_failing_tests", []))
    cascade_failures = post_patch_failing - baseline_failing_tests

    state["test_verification"] = "VERIFIED"

    # 1. Handle timeout
    if output == "TIMEOUT" or exit_code == -1:
        tests_passed = False
        state["validation_status"] = "ERROR"
        state["timeout"] = True
        state["cascade_failure"] = False
        state["retry_count"] = current_retry + 1
        logger.warning("VALIDATOR TIMEOUT — sandbox exceeded 120s")
        state["validator_summary"] = "Sandbox timed out after 120s."

    # 2. Cascade failure check
    elif cascade_failures:
        tests_passed = False
        state["validation_status"] = "FAILED"
        state["timeout"] = False
        state["cascade_failure"] = True
        state["cascade_failure_report"] = list(cascade_failures)
        state["retry_count"] = current_retry + 1
        state["confidence_score"] = round(max(30.0, 60.0 - (current_retry * 15.0)), 1)

        failing_test = state.get("failing_test", "the target test")
        state["cascade_context"] = f"Previous patch fixed {failing_test} but broke: {list(cascade_failures)}. Do not break these tests."
        state["cascade_report"] = (
            f"Your previous patch fixed {failing_test} but introduced {len(cascade_failures)} NEW test failures: "
            f"{list(cascade_failures)}. You MUST keep all existing tests passing. "
            f"Only change the minimum code needed to fix the bug."
        )

        logger.warning(f"CASCADE DETECTED — {len(cascade_failures)} tests newly broken: {list(cascade_failures)}")
        state["validator_summary"] = f"Original test fixed. BUT {len(cascade_failures)} new test failures introduced: {list(cascade_failures)}. Looping back."

    # 3. Handle PASS
    elif exit_code == 0:
        tests_passed = True
        state["validation_status"] = "PASSED"
        state["timeout"] = False
        state["cascade_failure"] = False
        state["retry_count"] = current_retry
        confidence = max(65.0, 95.0 - (current_retry * 10.0))
        state["confidence_score"] = round(confidence, 1)
        logger.info(f"VALIDATOR PASS — {repo_full_name} @ {commit_sha[:7]} — exit 0 — confidence: {state['confidence_score']}%")
        state["validator_summary"] = f"All tests passed in Docker sandbox. Confidence: {state['confidence_score']}%."

    # 4. Handle FAIL (no cascade, just standard failure)
    else:
        tests_passed = False
        state["validation_status"] = "FAILED"
        state["timeout"] = False
        state["cascade_failure"] = False
        state["retry_count"] = current_retry + 1
        logger.info(f"VALIDATOR FAIL — {repo_full_name} @ {commit_sha[:7]} — exit {exit_code} — retry {state['retry_count']}")
        state["validator_summary"] = f"Tests failed with exit code {exit_code}. Retry {state['retry_count']}."

    state["tests_passed"] = tests_passed
    state["test_output"] = output
    state["validator_exit_code"] = exit_code

    return state


def capture_baseline(state: Dict[str, Any]) -> Dict[str, Any]:
    """Captures the baseline state of the test suite before any patch is applied.

    In hosted mode: Skips running Docker sandbox to avoid crashing on Docker-free hosts.
    In local mode: Runs sandbox with no patch to record pre-existing test statuses.
    """
    if state.get("baseline_passing_tests") is not None:
        return state

    mode = state.get("execution_mode") or get_execution_mode()
    state["execution_mode"] = mode

    if mode == "hosted":
        logger.info("[Validator] Hosted mode — skipping Docker baseline test run")
        state["baseline_passing_tests"] = []
        state["baseline_failing_tests"] = []
        return state

    repo_full_name = state.get("repo_full_name", "")
    commit_sha = state.get("commit_sha", "")
    token = os.getenv("GITHUB_PAT")
    if token and repo_full_name:
        repo_url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
    else:
        repo_url = f"https://github.com/{repo_full_name}" if repo_full_name else ""

    sandbox_result = run_sandbox(repo_url, commit_sha, patch_diff=None)
    output = sandbox_result.get("output", "")

    if output and ("FAILED" in output or "ERROR" in output) and (not state.get("failure_log") or "FAILED" not in state.get("failure_log", "")):
        state["failure_log"] = output
        logger.info(f"BASELINE FAILURE LOG INJECTED into state ({len(output)} chars)")

    passing_tests = set()
    failing_tests = set()

    pattern = r"(\S+)[ \t]+(PASSED|FAILED)"
    for match in re.finditer(pattern, output):
        test_path = match.group(1)
        status = match.group(2)
        if status == "PASSED":
            passing_tests.add(test_path)
        elif status == "FAILED":
            failing_tests.add(test_path)

    state["baseline_passing_tests"] = list(passing_tests)
    state["baseline_failing_tests"] = list(failing_tests)

    logger.info(f"BASELINE CAPTURED — {len(passing_tests)} passing, {len(failing_tests)} failing")
    return state
