"""
test_pipeline.py — End-to-end integration sanity check for ARCANE pipeline.

Usage:
    python test_pipeline.py

This script is the team's shared sanity check — everyone should be able to run it.
It builds a mock CI failure event, runs the full LangGraph pipeline, and asserts
that the pipeline reaches DONE (not ESCALATED) with valid outputs.
"""

import asyncio
import json
import sys

from agents.orchestrator import run_pipeline


async def main():
    print("\n" + "=" * 70)
    print("  ARCANE Pipeline — End-to-End Integration Test")
    print("=" * 70)

    # ── 1. Build a mock CI failure event matching Step 1.1 webhook structure ──
    event = {
        "repo_full_name": "aryanketkar-15/ARCANE_Nexus2.0",
        "commit_sha": "a1b2c3d4e5f6789",
        "failure_log": (
            "FAILED tests/test_auth.py::test_login_validation - "
            "AssertionError: assert validate_token(None) is not None\n"
            "  File \"auth.py\", line 42, in validate_token\n"
            "    return token.strip()\n"
            "AttributeError: 'NoneType' object has no attribute 'strip'"
        ),
    }

    print(f"\n  Mock Event:")
    print(f"    repo       : {event['repo_full_name']}")
    print(f"    commit_sha : {event['commit_sha']}")
    print(f"    failure_log: {event['failure_log'][:80]}...")

    # ── 2. Run the full pipeline ──
    print("\n" + "-" * 70)
    print("  Running pipeline...")
    print("-" * 70 + "\n")

    final_state = await run_pipeline(event)

    # ── 3. Print full state dict ──
    print("\n" + "=" * 70)
    print("  FINAL STATE DUMP")
    print("=" * 70)
    for key, value in sorted(final_state.items()):
        val_str = str(value)
        if len(val_str) > 120:
            val_str = val_str[:120] + "..."
        print(f"  {key:25s}: {val_str}")

    # ── 4. Assertions ──
    print("\n" + "=" * 70)
    print("  ASSERTIONS")
    print("=" * 70)

    errors = []

    # Assert pipeline reached DONE (has pr_url, no escalation error pattern)
    pr_url = final_state.get("pr_url")
    if not pr_url:
        errors.append("FAIL: state['pr_url'] is missing — pipeline did not reach DONE/CREATING_PR")
    else:
        print(f"  [PASS] pr_url present: {pr_url}")

    # Assert patch_diff is non-empty
    patch_diff = final_state.get("patch_diff", "")
    if not patch_diff or not isinstance(patch_diff, str):
        errors.append("FAIL: state['patch_diff'] is empty or missing")
    else:
        print(f"  [PASS] patch_diff is non-empty ({len(patch_diff)} chars, {len(patch_diff.splitlines())} lines)")

    # Assert tests_passed is True
    tests_passed = final_state.get("tests_passed")
    if tests_passed is not True:
        errors.append(f"FAIL: state['tests_passed'] is {tests_passed}, expected True")
    else:
        print(f"  [PASS] tests_passed is True")

    # Assert no escalation error
    error = final_state.get("error")
    if error and "Escalated" in str(error):
        errors.append(f"FAIL: Pipeline escalated — error: {error}")
    else:
        print(f"  [PASS] No escalation error")

    # Assert confidence score exists
    confidence = final_state.get("confidence_score")
    if confidence is None:
        errors.append("FAIL: state['confidence_score'] is missing")
    else:
        print(f"  [PASS] confidence_score: {confidence}")

    # ── 5. Final verdict ──
    print("\n" + "=" * 70)
    if errors:
        print("  RESULT: FAILED")
        print("=" * 70)
        for e in errors:
            print(f"  [X] {e}")
        sys.exit(1)
    else:
        print("  RESULT: ALL ASSERTIONS PASSED [OK]")
        print("=" * 70)
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
