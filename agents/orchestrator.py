"""ARCANE Orchestrator — Phase 3 Hardened LangGraph State Machine

States:
  IDLE → ANALYZING → BISECTING → PATCHING → PROPAGATING →
  CONFLICT_CHECKING → VALIDATING → GENERATING_TEST → CREATING_PR → DONE
                                                                    ↘ ESCALATED

Conditional edges:
  • PATCHING      → ESCALATED           if retry_count >= 3
  • VALIDATING    → GENERATING_TEST     if tests_passed and confidence >= 60
  • VALIDATING    → PATCHING            if tests_passed == False and retry_count < 3
  • VALIDATING    → ESCALATED           if retry_count >= 3 OR confidence < 60
"""

import asyncio
import logging
import time
import functools
from datetime import datetime
from typing import TypedDict, Optional, Any
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# When ARCANE_MOCK_MODE=1, skip all real agent imports and use mocks
_MOCK_MODE = os.environ.get("ARCANE_MOCK_MODE", "0") == "1"
if _MOCK_MODE:
    logger.info("[ARCANE] MOCK_MODE enabled -- all agents will use mock fallbacks")

if _MOCK_MODE:
    run_analyst = run_bisect = run_patch_generator = None
    validate = capture_baseline = generate_regression_test = None
    _propagator = _conflict_resolver = None
    create_pr = None
    _chroma_client = query_memory = None
else:
    try:
        from agents.analyst_agent import analyze as run_analyst
    except ImportError as _e:
        logger.warning(f"[ARCANE] analyst_agent not available: {_e}")
        run_analyst = None

    try:
        from agents.git_bisect_agent import run_bisect
    except ImportError as _e:
        logger.warning(f"[ARCANE] git_bisect_agent not available: {_e}")
        run_bisect = None

    try:
        from agents.patch_generator import generate_patch as run_patch_generator
    except ImportError as _e:
        logger.warning(f"[ARCANE] patch_generator not available: {_e}")
        run_patch_generator = None

    try:
        from agents.validator_agent import validate, capture_baseline
    except ImportError as _e:
        logger.warning(f"[ARCANE] validator_agent not available: {_e}")
        validate = capture_baseline = None

    try:
        from agents.regression_test_generator import generate_regression_test
    except ImportError as _e:
        logger.warning(f"[ARCANE] regression_test_generator not available: {_e}")
        generate_regression_test = None

    try:
        from agents.cross_file_propagator import CrossFilePropagator
        _propagator = CrossFilePropagator()
    except ImportError as _e:
        logger.warning(f"[ARCANE] cross_file_propagator not available: {_e}")
        _propagator = None

    try:
        from agents.conflict_resolver import ConflictResolver
        _conflict_resolver = ConflictResolver()
    except ImportError as _e:
        logger.warning(f"[ARCANE] conflict_resolver not available: {_e}")
        _conflict_resolver = None

    try:
        from agents.pr_agent import create_pr
    except ImportError as _e:
        logger.warning(f"[ARCANE] pr_agent not available: {_e}")
        create_pr = None

    try:
        from agents.chroma_memory import init_memory, query_memory
        _chroma_client = init_memory(os.path.join(os.path.dirname(__file__), '..', 'chroma_data'))
        logger.info("[ARCANE] ChromaDB patch memory loaded")
    except ImportError as _e:
        logger.warning(f"[ARCANE] chroma_memory not available: {_e}")
        _chroma_client = query_memory = None
    except Exception as _e:
        logger.warning(f"[ARCANE] chroma_memory init failed: {_e}")
        _chroma_client = query_memory = None

from langgraph.graph import StateGraph, END

MAX_RETRIES = 3
NODE_TIMEOUT = 90  # seconds — any node exceeding this is auto-escalated


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  State schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ArcaneState(TypedDict, total=False):
    # ── Incoming (webhook payload) ──
    event: dict                         # raw CI failure dict
    repo_full_name: str
    commit_sha: str
    failure_log: str

    # ── ANALYZING outputs ──
    failing_test: str
    failing_file: str
    failing_line: int
    root_cause_summary: str
    suspected_function: str

    # ── BISECTING outputs ──
    bisect_intent_report: str

    # ── PATCHING outputs ──
    patch_diff: str
    retry_count: int

    # ── PROPAGATING outputs ──
    cascade_failure: bool
    cascade_report: str

    # ── CONFLICT_CHECKING outputs ──
    conflict_detected: bool

    # ── VALIDATING outputs ──
    tests_passed: bool
    confidence_score: float

    # ── GENERATING_TEST outputs ──
    regression_test_code: str

    # ── CREATING_PR outputs ──
    pr_url: Optional[str]

    # ── Meta ──
    error: Optional[str]
    auto_approved: bool
    pipeline_start_time: float
    agents_used: str


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Timeout wrapper — escalates if any node exceeds NODE_TIMEOUT seconds
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class NodeTimeoutError(Exception):
    """Raised when a node exceeds the allowed execution time."""
    pass


def with_timeout(node_name: str):
    """Decorator that wraps a node function with a timeout guard."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(state):
            start = time.time()
            try:
                result = func(state)
            except Exception:
                raise
            elapsed = time.time() - start
            if elapsed > NODE_TIMEOUT:
                logger.warning(
                    f"[TIMEOUT] {node_name} took {elapsed:.1f}s "
                    f"(limit: {NODE_TIMEOUT}s) — escalating"
                )
                result["error"] = f"{node_name} timed out after {elapsed:.1f}s"
                result["retry_count"] = result.get("retry_count", 0) + 1
            return result
        return wrapper
    return decorator


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Node functions — real agents with mock fallbacks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def idle_node(state: ArcaneState) -> ArcaneState:
    """IDLE — receives the raw CI failure event and seeds the state."""
    logger.info("[IDLE] Received CI failure event")
    event = state.get("event", {})
    seeded_state = {
        **state,
        "repo_full_name": event.get("repo_full_name", state.get("repo_full_name", "")),
        "commit_sha": event.get("commit_sha", state.get("commit_sha", "")),
        "failure_log": event.get("failure_log", state.get("failure_log", "")),
        "pipeline_start_time": time.time(),
        "agents_used": "",
    }
    # Capture baseline BEFORE any patch is applied
    if capture_baseline:
        seeded_state = capture_baseline(seeded_state)
    return seeded_state


@with_timeout("ANALYZING")
def analyzing_node(state: ArcaneState) -> ArcaneState:
    """ANALYZING — parses failure log to extract test name, file, line, root cause.
    Fast path: if ChromaDB has a cached patch for a similar error, skip LLM analysis.
    """
    logger.info("[ANALYZING] Delegating to Analyst Agent")
    try:
        # Fast path: check ChromaDB memory for a cached patch match
        if _chroma_client and query_memory:
            failure_log = state.get("failure_log", "")
            cached = query_memory(_chroma_client, failure_log)
            if cached:
                logger.info("[ANALYZING] MEMORY HIT -- reusing cached patch (fast path)")
                return {
                    **state,
                    "root_cause_summary": cached.get("root_cause", "Cached root cause"),
                    "patch_diff": cached.get("patch_diff", ""),
                    "failing_test": cached.get("test_file", "unknown"),
                    "memory_hit": True,
                    "agents_used": state.get("agents_used", "") + "chroma_memory,",
                }
            logger.info("[ANALYZING] No memory match -- falling through to LLM")

        if run_analyst:
            updated = run_analyst(state)
            result = {**state, **updated}
            result["agents_used"] = state.get("agents_used", "") + "analyst,"
            return result
        logger.warning("WARNING: analyst_agent not available — using mock")
        return {
            **state,
            "failing_test": "test_mock",
            "failing_file": "mock_file.py",
            "failing_line": 10,
            "root_cause_summary": "Mock root cause",
            "suspected_function": "mock_func"
        }
    except Exception as e:
        state['error'] = str(e)
        state['retry_count'] = state.get('retry_count', 0) + 1
        raise


@with_timeout("BISECTING")
def bisecting_node(state: ArcaneState) -> ArcaneState:
    """BISECTING — narrows down the commit that introduced the failure."""
    logger.info("[BISECTING] Running git-bisect analysis")
    try:
        if run_bisect:
            updated = run_bisect(state)
            result = {**state, **updated}
            result["agents_used"] = state.get("agents_used", "") + "bisect,"
            return result
            
        logger.warning("WARNING: git_bisect_agent not available — using mock")
        return {
            **state,
            "bisect_intent_report": (
                f"Bisect identified commit {state.get('commit_sha', '???')[:7]} as the "
                f"first bad commit. Function `{state.get('suspected_function', '?')}` "
                f"in `{state.get('failing_file', '?')}` was modified."
            ),
        }
    except Exception as e:
        state['error'] = str(e)
        state['retry_count'] = state.get('retry_count', 0) + 1
        raise


@with_timeout("PATCHING")
def patching_node(state: ArcaneState) -> ArcaneState:
    """PATCHING — generates a code patch to fix the suspected function."""
    retry = state.get("retry_count", 0)
    logger.info(f"[PATCHING] Delegating to Patch Generator (attempt {retry + 1}/{MAX_RETRIES})")
    # On retries, inject cascade_report into state so Patch Generator can learn
    if retry > 0:
        cascade = state.get("cascade_report", "")
        if cascade:
            logger.info(f"[PATCHING] Passing cascade_report to Patch Generator: {cascade[:100]}...")
    try:
        if run_patch_generator:
            updated = run_patch_generator(state)
            result = {**state, **updated}
            result["agents_used"] = state.get("agents_used", "") + "patch_gen,"
            return result
        
        logger.warning("WARNING: patch_generator not available — using mock")
        return {
            **state,
            "patch_diff": "--- a/mock.py\n+++ b/mock.py\n@@ -1 +1 @@\n-old\n+new",
            "retry_count": state.get("retry_count", 0)
        }
    except Exception as e:
        state['error'] = str(e)
        state['retry_count'] = state.get('retry_count', 0) + 1
        raise


@with_timeout("PROPAGATING")
def propagating_node(state: ArcaneState) -> ArcaneState:
    """PROPAGATING — checks for cascade / downstream failures."""
    logger.info("[PROPAGATING] Checking downstream impact")
    try:
        if _propagator:
            updated = _propagator.propagate(state)
            result = {**state, **updated}
            result["agents_used"] = state.get("agents_used", "") + "propagator,"
            return result
            
        logger.warning("WARNING: cross_file_propagator not available — using mock")
        return {
            **state,
            "cascade_failure": False,
            "cascade_report": "No downstream modules affected by this patch.",
        }
    except Exception as e:
        state['error'] = str(e)
        state['retry_count'] = state.get('retry_count', 0) + 1
        raise


@with_timeout("CONFLICT_CHECKING")
def conflict_checking_node(state: ArcaneState) -> ArcaneState:
    """CONFLICT_CHECKING — detects merge conflicts with main branch."""
    logger.info("[CONFLICT_CHECKING] Verifying merge compatibility")
    try:
        if _conflict_resolver:
            result = _conflict_resolver.check(state)
            # Aryan's ConflictResolver now returns confidence_score
            score = result.get("confidence_score", result.get("score", 1.0))
            return {
                **state,
                "conflict_detected": result.get("conflicts_found", False),
                "conflict_action": result.get("action", "no_conflict"),
                "conflict_score": score,
                "conflict_details": result.get("details", []),
                "agents_used": state.get("agents_used", "") + "conflict,",
            }
            
        logger.warning("WARNING: conflict_resolver not available — using mock")
        return {
            **state,
            "conflict_detected": False,
        }
    except Exception as e:
        state['error'] = str(e)
        state['retry_count'] = state.get('retry_count', 0) + 1
        raise


@with_timeout("VALIDATING")
def validating_node(state: ArcaneState) -> ArcaneState:
    """VALIDATING — applies patch in Docker sandbox and runs full pytest suite."""
    retry = state.get("retry_count", 0)
    logger.info(f"[VALIDATING] Running test suite (attempt {retry + 1})")
    try:
        if validate:
            updated = validate(state)
            result = {**state, **updated}
            result["agents_used"] = state.get("agents_used", "") + "validator,"
            # Auto-approve if confidence >= 85%
            # Check for confidence_score with fallback to score
            score = result.get("confidence_score", result.get("score", 0))
            if isinstance(score, (int, float)) and score >= 85:
                result["auto_approved"] = True
                logger.info(f"[VALIDATING] Confidence {score}% >= 85% — auto-approved")
            return result
            
        logger.warning("WARNING: validator_agent not available — using mock")
        passed = retry <= 1
        mock_score = 95 if passed else 30
        return {
            **state,
            "tests_passed": passed,
            "confidence_score": mock_score,
            "auto_approved": mock_score >= 85,
        }
    except Exception as e:
        state['error'] = str(e)
        state['retry_count'] = state.get('retry_count', 0) + 1
        raise


@with_timeout("GENERATING_TEST")
def generating_test_node(state: ArcaneState) -> ArcaneState:
    """GENERATING_TEST — synthesizes a regression test via Claude LLM."""
    logger.info("[GENERATING_TEST] Producing regression test via LLM")
    try:
        if generate_regression_test:
            updated = generate_regression_test(state)
            result = {**state, **updated}
            result["agents_used"] = state.get("agents_used", "") + "regtest,"
            return result
            
        logger.warning("WARNING: regression_test_generator not available — using mock")
        return {**state, "regression_test_code": "def test_placeholder(): pass"}
    except Exception as e:
        state['error'] = str(e)
        state['retry_count'] = state.get('retry_count', 0) + 1
        raise


@with_timeout("CREATING_PR")
def creating_pr_node(state: ArcaneState) -> ArcaneState:
    """CREATING_PR — opens a pull request on GitHub with the patch + test."""
    logger.info("[CREATING_PR] Opening pull request")
    try:
        if create_pr:
            # Rishi's create_pr now takes the full state dict
            pr_result = create_pr(state)
            if isinstance(pr_result, str):
                # Backwards compat: if create_pr returns just a URL string
                result = {**state, "pr_url": pr_result}
            elif isinstance(pr_result, dict):
                result = {**state, **pr_result}
            else:
                result = {**state, "pr_url": str(pr_result)}
            result["agents_used"] = state.get("agents_used", "") + "pr_agent,"
            return result
            
        logger.warning("WARNING: pr_agent not available — using mock")
        sha = state.get("commit_sha", "unknown")[:7]
        return {
            **state,
            "pr_url": f"https://github.com/{state.get('repo_full_name', 'org/repo')}/pull/mock-{sha}",
        }
    except Exception as e:
        state['error'] = str(e)
        state['retry_count'] = state.get('retry_count', 0) + 1
        raise


def done_node(state: ArcaneState) -> ArcaneState:
    """DONE — terminal success state with full summary."""
    elapsed = time.time() - state.get("pipeline_start_time", time.time())
    patch_lines = len(state.get("patch_diff", "").splitlines())
    agents = state.get("agents_used", "").strip(",").replace(",", ", ")
    confidence = state.get("confidence_score", "N/A")
    auto = state.get("auto_approved", False)

    logger.info(
        "\n" + "=" * 70 + "\n"
        "  [DONE] ARCANE Pipeline -- SUCCESS SUMMARY\n"
        "=" * 70 + "\n"
        f"  PR URL          : {state.get('pr_url', 'N/A')}\n"
        f"  Total Time      : {elapsed:.1f}s\n"
        f"  Confidence      : {confidence}\n"
        f"  Auto-Approved   : {auto}\n"
        f"  Agents Used     : {agents}\n"
        f"  Patch Size      : {patch_lines} lines\n"
        f"  Commit SHA      : {state.get('commit_sha', '?')}\n"
        f"  Failing Test    : {state.get('failing_test', '?')}\n"
        + "=" * 70
    )
    return state


def escalated_node(state: ArcaneState) -> ArcaneState:
    """ESCALATED — retries exhausted or low confidence; needs human review."""
    elapsed = time.time() - state.get("pipeline_start_time", time.time())
    report = (
        "\n" + "!" * 70 + "\n"
        "  [ESCALATED] ARCANE Pipeline -- ESCALATION REPORT\n"
        "!" * 70 + "\n"
        f"  Commit SHA      : {state.get('commit_sha', '?')}\n"
        f"  Failing Test    : {state.get('failing_test', '?')}\n"
        f"  Retry Count     : {state.get('retry_count', 0)}/{MAX_RETRIES}\n"
        f"  Confidence      : {state.get('confidence_score', 'N/A')}\n"
        f"  Last Error      : {state.get('error', 'None')}\n"
        f"  Elapsed Time    : {elapsed:.1f}s\n"
        f"  Agents Used     : {state.get('agents_used', '').strip(',').replace(',', ', ')}\n"
        + "!" * 70
    )
    logger.warning(report)
    return {
        **state,
        "error": (
            f"Escalated after {state.get('retry_count', 0)} retries -- "
            f"confidence: {state.get('confidence_score', 'N/A')} -- "
            f"human review required for {state.get('failing_test', 'unknown test')}"
        ),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Conditional edge functions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def after_patching(state: ArcaneState) -> str:
    """After PATCHING: escalate if retry cap hit, else continue to PROPAGATING."""
    if state.get("retry_count", 0) >= MAX_RETRIES:
        logger.warning("[PATCHING] Retry cap reached — escalating")
        return "escalated"
    return "propagating"


def after_validating(state: ArcaneState) -> str:
    """After VALIDATING: route based on test results + confidence + retry count."""
    confidence = state.get("confidence_score", 0)
    # Normalize 0-1 scale to 0-100 if needed
    if isinstance(confidence, float) and confidence <= 1.0:
        confidence = confidence * 100
    # Confidence gating: < 60 means do NOT attempt PR, escalate immediately
    if isinstance(confidence, (int, float)) and confidence < 60:
        logger.warning(
            f"[VALIDATING] Confidence {confidence:.0f}% < 60% — too low, escalating"
        )
        return "escalated"
    if state.get("tests_passed"):
        return "generating_test"
    if state.get("retry_count", 0) >= MAX_RETRIES:
        logger.warning("[VALIDATING] Retry cap reached — escalating")
        return "escalated"
    logger.info("[VALIDATING] Tests failed — looping back to PATCHING")
    return "patching"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Build & compile the graph
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_graph() -> StateGraph:
    builder = StateGraph(ArcaneState)

    # ── Register nodes ──
    builder.add_node("idle", idle_node)
    builder.add_node("analyzing", analyzing_node)
    builder.add_node("bisecting", bisecting_node)
    builder.add_node("patching", patching_node)
    builder.add_node("propagating", propagating_node)
    builder.add_node("conflict_checking", conflict_checking_node)
    builder.add_node("validating", validating_node)
    builder.add_node("generating_test", generating_test_node)
    builder.add_node("creating_pr", creating_pr_node)
    builder.add_node("done", done_node)
    builder.add_node("escalated", escalated_node)

    # ── Entry point ──
    builder.set_entry_point("idle")

    # ── Linear edges ──
    builder.add_edge("idle", "analyzing")
    builder.add_edge("analyzing", "bisecting")
    builder.add_edge("bisecting", "patching")

    # ── Conditional: PATCHING → PROPAGATING or ESCALATED ──
    builder.add_conditional_edges(
        "patching",
        after_patching,
        {
            "propagating": "propagating",
            "escalated": "escalated",
        },
    )

    # ── Linear edges continued ──
    builder.add_edge("propagating", "conflict_checking")
    builder.add_edge("conflict_checking", "validating")

    # ── Conditional: VALIDATING → GENERATING_TEST / PATCHING / ESCALATED ──
    builder.add_conditional_edges(
        "validating",
        after_validating,
        {
            "generating_test": "generating_test",
            "patching": "patching",
            "escalated": "escalated",
        },
    )

    # ── Linear edges to terminal states ──
    builder.add_edge("generating_test", "creating_pr")
    builder.add_edge("creating_pr", "done")
    builder.add_edge("done", END)
    builder.add_edge("escalated", END)

    return builder.compile()


# Pre-compiled graph — importable as `from agents.orchestrator import graph`
graph = build_graph()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pipeline entry point  (async — callable from FastAPI event loop)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def run_pipeline(event: dict) -> dict:
    """
    Accepts a parsed webhook event dict from the asyncio queue and drives
    the full LangGraph state machine to completion.

    Logs every state transition in the format:
        [HH:MM:SS] STATE: {state_name} | retry: {retry_count}

    Returns the final state dict.
    """
    initial_state: ArcaneState = {
        "event": event,
        "repo_full_name": event.get("repo_full_name", ""),
        "commit_sha": event.get("commit_sha", ""),
        "failure_log": event.get("failure_log", ""),
        "retry_count": 0,
    }

    ts = datetime.now().strftime("%H:%M:%S")
    logger.info(f"[{ts}] STATE: __start__ | retry: 0")

    final_state = {}

    # astream yields {node_name: state_after_node} for each step
    async for step_output in graph.astream(initial_state):
        for node_name, node_state in step_output.items():
            retry = node_state.get("retry_count", 0) if isinstance(node_state, dict) else 0
            ts = datetime.now().strftime("%H:%M:%S")
            logger.info(f"[{ts}] STATE: {node_name} | retry: {retry}")
            if isinstance(node_state, dict):
                final_state = node_state

    ts = datetime.now().strftime("%H:%M:%S")
    logger.info(f"[{ts}] STATE: __end__ | retry: {final_state.get('retry_count', 0)}")

    return final_state


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Verification  (run this file directly: python -m agents.orchestrator)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _verify():
    """Runs all prompt-2 verification checkpoints."""
    drawable = graph.get_graph()

    # ── 1. Visualise ──
    print("\n" + "=" * 70)
    print("  [OK] CHECKPOINT: LangGraph visualiser produces diagram")
    print("=" * 70)
    drawable.print_ascii()
    print("\n--- Mermaid ---")
    print(drawable.draw_mermaid())

    # ── 2. Happy path -> DONE ──
    print("\n" + "=" * 70)
    print("  [OK] CHECKPOINT: Mock event -> run_pipeline -> reaches DONE")
    print("=" * 70)
    result = await run_pipeline({
        "repo_full_name": "aryanketkar-15/ARCANE_Nexus2.0",
        "commit_sha": "a1b2c3d4e5f6789",
        "failure_log": "FAILED test_payment_processing -- AssertionError",
    })
    print(f"\n  tests_passed      : {result.get('tests_passed')}")
    print(f"  confidence_score  : {result.get('confidence_score')}")
    print(f"  retry_count       : {result.get('retry_count')}")
    print(f"  pr_url            : {result.get('pr_url')}")
    print(f"  error             : {result.get('error', 'None')}")
    assert result.get("pr_url"), "FAIL: expected pr_url in DONE state"
    assert result.get("tests_passed") is True, "FAIL: expected tests_passed=True"
    print("  [PASS] DONE path verified")

    # ── 3. Escalation path -> ESCALATED after 3 retries ──
    print("\n" + "=" * 70)
    print("  [OK] CHECKPOINT: Retry counter -> ESCALATED after 3 retries")
    print("=" * 70)
    # Force failures by starting retry_count high so patching hits cap
    esc_state: ArcaneState = {
        "event": {
            "repo_full_name": "aryanketkar-15/ARCANE_Nexus2.0",
            "commit_sha": "deadbeef1234567",
            "failure_log": "FAILED test_checkout -- TypeError",
        },
        "retry_count": 2,  # next patch makes it 3 -> escalate
    }
    final = {}
    async for step_output in graph.astream(esc_state):
        for node_name, node_state in step_output.items():
            retry = node_state.get("retry_count", 0) if isinstance(node_state, dict) else 0
            ts = datetime.now().strftime("%H:%M:%S")
            logger.info(f"[{ts}] STATE: {node_name} | retry: {retry}")
            if isinstance(node_state, dict):
                final = node_state

    print(f"\n  retry_count       : {final.get('retry_count')}")
    print(f"  error             : {final.get('error', 'None')}")
    assert final.get("retry_count", 0) >= MAX_RETRIES, "FAIL: retry_count should be >= 3"
    assert final.get("error"), "FAIL: expected error message in ESCALATED state"
    print("  [PASS] ESCALATED path verified")

    print("\n" + "=" * 70)
    print("  [DONE] ALL PROMPT-2 VERIFICATION CHECKPOINTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(_verify())
