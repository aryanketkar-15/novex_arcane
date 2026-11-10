"""Translates LangGraph node results into dashboard run records."""
import logging
import re
from typing import Optional

from api.store import store, now_iso

NODE_STAGE = {
    "idle": "QUEUED",
    "analyzing": "ANALYZING",
    "bisecting": "BISECTING",
    "patching": "PATCHING",
    "propagating": "PROPAGATING",
    "conflict_checking": "CONFLICT CHECK",
    "validating": "VALIDATING",
    "generating_test": "GENERATING TEST",
    "creating_pr": "CREATING PR",
    "done": "DONE",
    "escalated": "ESCALATED",
}

# What is running *after* the given node finishes
NEXT_RUNNING = {
    "idle": ("failed", "Reading the failed workflow and changed files"),
    "analyzing": ("bisect", "Replaying history to find the breaking commit"),
    "bisecting": ("patch", "Generating a candidate patch"),
    "patching": ("related", "Checking related call sites"),
    "propagating": ("related", "Checking merge compatibility"),
    "conflict_checking": ("sandbox", "Running tests in a Docker sandbox"),
    "validating": ("ready", "Writing regression test and pull request"),
}


def norm_confidence(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 1.0:
        v *= 100
    return max(0, min(100, round(v)))


def _short(s: str, n: int = 160) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def diff_stats(diff: str) -> dict:
    files, add, dele = [], 0, 0
    cur = None
    for line in (diff or "").splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            path = path[2:] if path.startswith("b/") else path
            cur = {"path": path, "add": 0, "del": 0}
            files.append(cur)
        elif line.startswith("--- "):
            continue
        elif line.startswith("+") and not line.startswith("+++"):
            add += 1
            if cur:
                cur["add"] += 1
        elif line.startswith("-") and not line.startswith("---"):
            dele += 1
            if cur:
                cur["del"] += 1
    return {"files": files, "add": add, "del": dele}


class RunLogHandler(logging.Handler):
    """Captures python log records emitted while a run is executing."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.run: Optional[dict] = None

    def emit(self, record: logging.LogRecord) -> None:
        run = self.run
        if run is None:
            return
        try:
            msg = record.getMessage()
        except Exception:
            return
        if record.name.startswith(("uvicorn", "httpx", "httpcore", "chromadb", "urllib3")):
            return
        lowered = msg.lower()
        if "path used" in lowered:
            m = re.search(r"\(([^)]+)\)", msg)
            if m:
                model_id = m.group(1)
                run["model"] = f"{model_id} (local)" if "ollama" in lowered else model_id
            elif "mock" in lowered:
                run["model"] = "offline mock"
        level = "ERROR" if record.levelno >= logging.ERROR else "WARN" if record.levelno >= logging.WARNING else "INFO"
        store.log(run, level, msg)


run_log_handler = RunLogHandler()


def start_run(run: dict) -> None:
    run["status"] = "working"
    run["stage"] = "ANALYZING"
    run["title"] = "Analyzing build failure"
    store.set_step(run, "failed", "running", "Reading the failed workflow and changed files")
    store.log(run, "INFO", f"Repair started for {run['repo']} @ {run['sha'][:7] or '?'}")
    store.save()


def apply_node(run: dict, node: str, state: dict) -> None:
    """Merge the state emitted by ``node`` into the run and advance the timeline."""
    run["stage"] = NODE_STAGE.get(node, node.upper())
    run["attempts"] = max(run.get("attempts", 0), int(state.get("retry_count", 0) or 0) + (1 if node in ("patching", "validating") else 0))

    if state.get("execution_mode"):
        run["executionMode"] = state["execution_mode"]
    if state.get("validation_status"):
        run["validationStatus"] = state["validation_status"]
    if state.get("test_verification"):
        run["testVerification"] = state["test_verification"]
    if state.get("is_draft_pr") is not None:
        run["isDraftPr"] = bool(state["is_draft_pr"])

    if state.get("agents_used"):
        run["agentsUsed"] = [a for a in str(state["agents_used"]).split(",") if a]

    if node == "analyzing":
        run["failingTest"] = state.get("failing_test", "") or run["failingTest"]
        run["failingFile"] = state.get("failing_file", "") or run["failingFile"]
        run["failingLine"] = state.get("failing_line", 0) or 0
        run["suspectedFunction"] = state.get("suspected_function", "") or ""
        run["rootCause"] = state.get("root_cause_summary", "") or run["rootCause"]
        run["memoryHit"] = bool(state.get("fast_forward_patch"))
        if state.get("patch_diff") and run["memoryHit"]:
            run["patchDiff"] = state["patch_diff"]
        if run["rootCause"]:
            run["title"] = _short(run["rootCause"], 90)
        ev = run["failingTest"] and f"{run['failingTest']} failed" or ""
        store.set_step(run, "failed", "failed", _short(run["rootCause"]) or "The build failed.", ev)
        if run["memoryHit"]:
            store.log(run, "INFO", "Memory hit — reusing a previously approved fix")
            for k in ("bisect", "patch", "related", "sandbox"):
                store.set_step(run, k, "done", "Skipped — reused a past fix from memory.")

    elif node == "bisecting":
        run["bisectReport"] = state.get("bisect_intent_report", "") or ""
        store.set_step(run, "bisect", "done", _short(run["bisectReport"]) or "Bisect finished.", f"commit {run['sha'][:7]}")

    elif node == "patching":
        run["patchDiff"] = state.get("patch_diff", "")
        run["patchedContent"] = state.get("patched_content", "")
        if not run["patchDiff"].strip():
            store.set_step(run, "patch", "failed", state.get("error") or "No source changes were generated.")
            store.save()
            return
        st = diff_stats(run["patchDiff"])
        ev = f"{len(st['files'])} files changed · {st['add']} additions · {st['del']} deletions"
        store.set_step(run, "patch", "done", f"Attempt {run['attempts']} produced a candidate patch.", ev)

    elif node == "propagating":
        run["cascadeReport"] = state.get("cascade_report", "") or ""
        store.set_step(run, "related", "running", _short(run["cascadeReport"]) or "Reviewed related call sites.")

    elif node == "conflict_checking":
        detected = bool(state.get("conflict_detected"))
        details = state.get("conflict_details", []) or []
        scores = [d.get("score") for d in details if isinstance(d, dict) and isinstance(d.get("score"), (int, float))]
        run["conflict"] = {
            "detected": detected,
            "action": state.get("conflict_action", "no_conflict"),
            "score": min(scores) if scores else 1.0,
            "details": details,
        }
        body = "Merge conflict found — resolved with shared-intent analysis." if detected else "No merge conflicts with the target branch."
        if run["conflict"]["action"] == "unverified":
            body = "Merge compatibility unverified."
        store.set_step(run, "related", "done", (run["cascadeReport"] and _short(run["cascadeReport"]) + " · " or "") + body)

    elif node == "validating":
        run["testsPassed"] = state.get("tests_passed")
        run["confidence"] = norm_confidence(state.get("confidence_score"))
        run["autoApproved"] = bool(state.get("auto_approved"))
        run["validatorSummary"] = state.get("validator_summary", "") or ""
        run["validationStatus"] = state.get("validation_status", "PASSED" if run["testsPassed"] else "FAILED")
        run["testVerification"] = state.get("test_verification", "VERIFIED")
        
        if run["validationStatus"] == "SKIPPED":
            store.set_step(
                run,
                "sandbox",
                "done",
                _short(run["validatorSummary"]) or "Docker sandbox skipped in hosted cloud environment.",
                "unverified · tests not run · static checks passed",
            )
        else:
            passed = bool(run["testsPassed"])
            conf = f"{run['confidence']}% confidence" if run["confidence"] is not None else "confidence n/a"
            store.set_step(
                run,
                "sandbox",
                "done" if passed else "failed",
                _short(run["validatorSummary"]) or ("Tests passed." if passed else "Tests failed."),
                f"{'passed' if passed else 'failed'} · {conf} · network disabled",
            )

    elif node == "generating_test":
        run["regressionTest"] = state.get("regression_test_code", "") or ""

    elif node == "creating_pr":
        run["prUrl"] = state.get("pr_url") or run["prUrl"]

    nxt = NEXT_RUNNING.get(node)
    if nxt and run["status"] == "working":
        key, text = nxt
        cur = next((s for s in run["steps"] if s["key"] == key), None)
        if cur and cur["status"] in ("pending", "done") and node != "idle":
            if cur["status"] == "pending":
                store.set_step(run, key, "running", text)
    store.save()


def finish_run(run: dict, final: dict, error: Optional[str] = None) -> None:
    safety = store.settings.get("safety", {})
    min_conf = int(safety.get("minConfidence", 85))
    esc_below = int(safety.get("escalateBelow", 60))

    run["finishedAt"] = now_iso()
    if final.get("pr_url"):
        run["prUrl"] = final["pr_url"]
    if final.get("regression_test_code"):
        run["regressionTest"] = final["regression_test_code"]
    if final.get("confidence_score") is not None:
        run["confidence"] = norm_confidence(final.get("confidence_score"))
    if final.get("tests_passed") is not None:
        run["testsPassed"] = final.get("tests_passed")
    if final.get("execution_mode"):
        run["executionMode"] = final.get("execution_mode")
    if final.get("validation_status"):
        run["validationStatus"] = final.get("validation_status")
    if final.get("test_verification"):
        run["testVerification"] = final.get("test_verification")
    if final.get("is_draft_pr") is not None:
        run["isDraftPr"] = bool(final.get("is_draft_pr"))
    err = error or final.get("error")

    if error and not run["prUrl"]:
        run["status"] = "failed"
        run["error"] = error
        run["reason"] = _short(error, 200)
        run["stage"] = "FAILED"
        run["title"] = run["title"] or "Repair failed"
        for s in run["steps"]:
            if s["status"] in ("running", "pending"):
                s["status"] = "failed" if s["status"] == "running" else "pending"
                break
        store.set_step(run, "ready", "failed", "ARCANE hit an error and stopped.", _short(error, 120))
        store.log(run, "ERROR", f"Run failed: {error}")
    elif run["stage"] == "ESCALATED" or (not run["prUrl"] and err):
        run["status"] = "person"
        run["error"] = err
        run["reason"] = _short(err or "Confidence below safety rule", 200)
        store.set_step(run, "ready", "failed", "ARCANE could not prove this fix is safe — a person should decide.", _short(err or "", 120))
        store.log(run, "WARN", f"Escalated to a person: {run['reason']}")
    else:
        conf = run["confidence"]
        run["status"] = "ready"
        if not run["patchDiff"].strip():
            run["status"] = "person"
            run["reason"] = "ARCANE did not produce a patch (the failing file was not available locally)"
        elif run.get("validationStatus") == "SKIPPED" or run.get("isDraftPr"):
            run["reason"] = ("Draft PR open on GitHub." if run["prUrl"] else "Patch prepared; no GitHub PR opened.") + " Docker tests were not run."
        elif conf is not None and conf < esc_below:
            run["status"] = "person"
            run["reason"] = f"Confidence {conf}% is below the {esc_below}% safety rule"
        elif conf is not None and conf < min_conf:
            run["reason"] = f"Confidence {conf}% is under the {min_conf}% auto-approve rule — review before merging"
        
        is_draft = bool(run.get("isDraftPr") or run.get("validationStatus") == "SKIPPED")
        store.set_step(
            run,
            "ready",
            "done" if run["status"] == "ready" else "failed",
            run["reason"] if is_draft and run["status"] == "ready" else ("The fix is safe to open as a pull request." if run["status"] == "ready" else run["reason"]),
            "draft PR · review required" if is_draft else (f"{conf}% confidence" if conf is not None else ""),
        )
        store.log(run, "INFO", f"Run finished — status={run['status']} (draft_pr={is_draft})")

    run["stage"] = "DONE" if run["status"] == "ready" else run["stage"]
    store.add_audit(
        "Prepared unverified draft PR" if run.get("isDraftPr") else ({"ready": "Prepared a tested fix", "person": "Sent fix to a person", "failed": "Repair failed"}.get(run["status"], "Finished run")),
        run["repo"],
        "ARCANE",
    )
    store.save()


def build_state_from_run(run: dict) -> dict:
    """Rebuild the orchestrator state needed by pr_agent."""
    return {
        "repo_full_name": run["repo"],
        "commit_sha": run["sha"],
        "failure_log": run["failureLog"],
        "patch_diff": run["patchDiff"],
        "patched_content": run.get("patchedContent", ""),
        "event": {"branch": run["branch"]},
        "failing_test": run["failingTest"],
        "failing_file": run["failingFile"],
        "root_cause_summary": run["rootCause"],
        "regression_test_code": run["regressionTest"],
        "confidence_score": run["confidence"],
        "tests_passed": bool(run["testsPassed"]),
        "execution_mode": run.get("executionMode", "auto"),
        "validation_status": run.get("validationStatus"),
        "test_verification": run.get("testVerification"),
        "is_draft_pr": run.get("isDraftPr", False),
    }
