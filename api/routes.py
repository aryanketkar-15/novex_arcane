"""REST API consumed by the ARCANE dashboard (new_frontend)."""
import asyncio
import logging
import os
import re
import threading
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.auth import require_user
from api.recorder import build_state_from_run, diff_stats
from api.store import now_iso, store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])

START_TIME = time.time()

_token_lock = threading.Lock()


class github_token:
    """Temporarily exposes a user's GitHub token to the agents (they read GITHUB_PAT)."""

    def __init__(self, token):
        self.token = token

    def __enter__(self):
        _token_lock.acquire()
        self.prev = os.environ.get("GITHUB_PAT")
        if self.token:
            os.environ["GITHUB_PAT"] = self.token

    def __exit__(self, *a):
        if self.prev is None:
            os.environ.pop("GITHUB_PAT", None)
        else:
            os.environ["GITHUB_PAT"] = self.prev
        _token_lock.release()


# ── helpers ──────────────────────────────────────────────────────────────────


def login_of(user) -> str:
    return (user or {}).get("login", "local")


def visible(run: dict, user) -> bool:
    """With OAuth on, users only see their own runs."""
    return user is None or run.get("owner") == user.get("login")


def get_visible(run_id: str, user) -> dict:
    run = store.get_run(run_id)
    if not run or not visible(run, user):
        raise HTTPException(404, "Run not found")
    return run


def public_run(run: dict, detail: bool = False) -> dict:
    out = {k: v for k, v in run.items() if k not in ("logs", "failureLog", "patchDiff", "regressionTest", "patchedContent")}
    st = diff_stats(run.get("patchDiff", ""))
    out["diffSummary"] = {"files": len(st["files"]), "add": st["add"], "del": st["del"]}
    out["logCount"] = len(run.get("logs", []))
    if detail:
        out["logs"] = run.get("logs", [])
        out["failureLog"] = run.get("failureLog", "")
        out["patchDiff"] = run.get("patchDiff", "")
        out["diffFiles"] = st["files"]
        out["regressionTest"] = run.get("regressionTest", "")
    return out


def parse_workflow_ref(text: str) -> tuple[Optional[str], Optional[str]]:
    """Accepts a GitHub Actions URL or numeric ID -> (repo_full_name, run_id)."""
    text = (text or "").strip()
    m = re.search(r"github\.com/([^/\s]+/[^/\s]+)/actions/runs/(\d+)", text)
    if m:
        return m.group(1), m.group(2)
    if text.isdigit():
        return None, text
    return None, None


def fetch_github_run(repo: str, run_id: str, token: Optional[str] = None) -> dict:
    """Best-effort enrichment from the GitHub API. Never raises."""
    if not token:
        from api.auth import token_for_repo
        token = os.getenv("GITHUB_PAT") or token_for_repo(repo)
    if not token or not repo:
        return {}
    try:
        from github import Github, Auth

        g = Github(auth=Auth.Token(token), timeout=10)
        wf = g.get_repo(repo).get_workflow_run(int(run_id))
        failed = []
        failure_log_text = ""
        try:
            for job in wf.jobs():
                if job.conclusion == "failure":
                    steps = [s.name for s in job.steps if s.conclusion == "failure"]
                    failed.append(f"Job '{job.name}' failed" + (f" at step(s): {', '.join(steps)}" if steps else ""))
                    from agents.github_diagnostics import job_diagnostics
                    if len(failed) <= 4:
                        try:
                            excerpt = job_diagnostics(repo, job.id, token)
                            failure_log_text += f"\nJob {job.name}:\n{excerpt}"
                        except Exception:
                            failed.append(f"Job {job.id} log unavailable (expired or inaccessible).")
        except Exception:
            pass
        head = wf.head_commit
        return {
            "conclusion": wf.conclusion,
            "sha": wf.head_sha,
            "branch": wf.head_branch,
            "message": head.message if head else "",
            "author": head.author.name if head and head.author else "",
            "summary": "\n".join(failed),
            "log": failure_log_text,
            "name": wf.name,
        }
    except Exception as e:
        logger.warning(f"[API] Could not enrich GitHub run {run_id}: {e}")
        return {}


def enqueue_event(request_or_app: Any, event: dict) -> dict:
    """Create a run record and push the event to the pipeline queue."""
    app = getattr(request_or_app, "app", request_or_app)

    # Free-tier memory protection: prevent overlapping repair runs
    from agents.execution_mode import is_hosted_mode
    if is_hosted_mode() and getattr(app.state, "is_repair_active", False) and getattr(app.state, "event_queue", None) and app.state.event_queue.qsize() >= 1:
        raise HTTPException(
            status_code=429,
            detail="A repair is already in progress. Concurrent repairs are limited to 1 on this instance to conserve memory. Please wait for the current repair to complete.",
        )

    run = store.create_run(event)
    event["run_id"] = run["id"]
    try:
        app.state.event_queue.put_nowait(event)
    except asyncio.QueueFull:
        run["status"] = "failed"
        run["error"] = "Queue is full — too many repairs in flight."
        run["finishedAt"] = now_iso()
        store.save()
        raise HTTPException(status_code=429, detail=run["error"])
    store.log(run, "INFO", "Event queued")
    store.add_audit("Queued repair", run["repo"], event.get("actor", "ARCANE"))
    return run


# ── overview ─────────────────────────────────────────────────────────────────

@router.get("/overview")
def overview(user=Depends(require_user)):
    runs = [r for r in store.runs if visible(r, user)]
    from api.main_state import pipeline_status  # lazy to avoid cycles

    week_ago = time.time() - 7 * 86400

    def ts(r):
        try:
            from datetime import datetime

            return datetime.fromisoformat(r["startedAt"]).timestamp()
        except Exception:
            return 0

    week = [r for r in runs if ts(r) >= week_ago]
    finished = [r for r in runs if r.get("finishedAt")]
    durations = []
    for r in finished:
        try:
            from datetime import datetime

            durations.append(
                (datetime.fromisoformat(r["finishedAt"]) - datetime.fromisoformat(r["startedAt"])).total_seconds()
            )
        except Exception:
            pass
    return {
        "pipeline": pipeline_status,
        "counts": {
            "all": len(runs),
            "ready": sum(1 for r in runs if r["status"] == "ready"),
            "working": sum(1 for r in runs if r["status"] == "working"),
            "person": sum(1 for r in runs if r["status"] == "person"),
            "failed": sum(1 for r in runs if r["status"] == "failed"),
        },
        "week": {
            "prepared": sum(1 for r in week if r["status"] in ("ready", "person")),
            "merged": sum(1 for r in week if r.get("decision") == "accepted"),
            "sentBack": sum(1 for r in week if r.get("decision") == "rejected"),
        },
        "avgSeconds": round(sum(durations) / len(durations)) if durations else None,
        "uptime": round(time.time() - START_TIME),
        "serverTime": now_iso(),
    }


# ── runs ─────────────────────────────────────────────────────────────────────

@router.get("/runs")
def list_runs(status: str = "", repo: str = "", q: str = "", limit: int = 100, user=Depends(require_user)):
    out = []
    ql = q.lower().strip()
    for r in store.runs:
        if not visible(r, user):
            continue
        if status and r["status"] != status:
            continue
        if repo and r["repo"] != repo:
            continue
        if ql and ql not in " ".join([r["repo"], r["failingTest"], r["sha"], r["title"], r["branch"]]).lower():
            continue
        out.append(public_run(r))
        if len(out) >= limit:
            break
    return {"runs": out}


@router.get("/runs/{run_id}")
def get_run(run_id: str, user=Depends(require_user)):
    run = get_visible(run_id, user)
    return public_run(run, detail=True)


class NewRun(BaseModel):
    repo: str = ""
    branch: str = "main"
    workflowRun: str = ""
    context: str = ""
    failureLog: str = ""
    sha: str = ""


@router.post("/runs")
def create_run(body: NewRun, request: Request, user=Depends(require_user)):
    repo = body.repo.strip()
    url_repo, run_id = parse_workflow_ref(body.workflowRun)
    repo = repo or url_repo or ""
    if not repo or "/" not in repo:
        raise HTTPException(400, "Repository must look like owner/name.")
    if body.workflowRun and not run_id:
        raise HTTPException(400, "Expected a GitHub Actions run URL or numeric ID.")
    if url_repo and url_repo.lower() != repo.lower():
        raise HTTPException(400, "Workflow URL and selected repository disagree.")
    from api.auth import token_for_login, token_for_repo
    user_token = (user or {}).get("token") or token_for_login(login_of(user)) or token_for_repo(repo)
    gh = fetch_github_run(repo, run_id, token=user_token) if run_id else {}
    if run_id and not gh.get("sha"):
        raise HTTPException(422, "Cannot read this workflow run. Check the repository, run ID and Actions read access.")
    if run_id and gh.get("conclusion") not in ("failure", "timed_out", "startup_failure"):
        raise HTTPException(422, "Select a completed, failed workflow run.")
    sha = gh.get("sha") or body.sha.strip()
    log_parts = [p for p in (body.failureLog, gh.get("log"), gh.get("summary"), gh.get("message"), body.context) if p]
    failure_log = "\n".join(log_parts) or f"Manual repair requested for {repo} on {body.branch}"
    event = {
        "repo_full_name": repo,
        "commit_sha": sha or f"manual{int(time.time()) % 10_000_000:07d}",
        "failure_log": failure_log,
        "branch": gh.get("branch") or body.branch or "main",
        "commit_message": gh.get("message", ""),
        "author": gh.get("author", ""),
        "source": "manual",
        "hint": body.context,
        "workflow_run": body.workflowRun,
        "actor": login_of(user),
        "owner": login_of(user) if user else "",
    }
    run = enqueue_event(request, event)
    return public_run(run, detail=True)


class RetryBody(BaseModel):
    hint: str = ""
    files: list[str] = []
    model: str = ""


@router.post("/runs/{run_id}/retry")
def retry_run(run_id: str, body: RetryBody, request: Request, user=Depends(require_user)):
    old = get_visible(run_id, user)
    log = old["failureLog"]
    if body.hint:
        log += f"\n\nHint from reviewer: {body.hint}"
    if body.files:
        log += f"\nFocus on files: {', '.join(body.files)}"
    event = {
        "repo_full_name": old["repo"],
        "commit_sha": old["sha"],
        "failure_log": log,
        "branch": old["branch"],
        "commit_message": old["commitMessage"],
        "author": old["author"],
        "source": "retry",
        "hint": body.hint,
        "workflow_run": old.get("workflowRun", ""),
        "actor": login_of(user),
        "owner": old.get("owner", ""),
    }
    run = enqueue_event(request, event)
    store.add_audit("Retried with a hint", old["repo"], login_of(user))
    return public_run(run, detail=True)


class PrBody(BaseModel):
    title: str = ""
    comment: str = ""


@router.post("/runs/{run_id}/open-pr")
def open_pr(run_id: str, body: PrBody, user=Depends(require_user)):
    run = get_visible(run_id, user)
    if run["status"] == "working":
        raise HTTPException(409, "This repair is still running.")
    if not run.get("patchDiff", "").strip():
        raise HTTPException(409, "No source patch is available.")
    if not run["prUrl"]:
        try:
            if os.getenv("ARCANE_DRY_RUN", "").lower() in ("1", "true", "yes"):
                raise HTTPException(409, "Dry run is enabled; no GitHub PR was opened.")
            else:
                from agents.pr_agent import create_pr
                from api.auth import token_for_login

                tok = (user or {}).get("token") or token_for_login(run.get("owner", ""))
                with github_token(tok):
                    run["prUrl"] = create_pr(build_state_from_run(run))
        except HTTPException:
            raise
        except Exception as e:
            store.log(run, "ERROR", f"Open PR failed: {e}")
            store.save()
            raise HTTPException(502, f"Could not open the pull request: {e}")
        store.log(run, "INFO", f"Pull request opened: {run['prUrl']}")
    run["decision"] = "accepted"
    store.add_audit("Opened pull request", run["repo"], login_of(user))
    store.save()
    return {"prUrl": run["prUrl"], "run": public_run(run)}


class DecisionBody(BaseModel):
    action: str  # accept | reject | assign
    reason: str = ""
    note: str = ""
    assignee: str = ""


@router.post("/runs/{run_id}/decision")
def decide(run_id: str, body: DecisionBody, user=Depends(require_user)):
    run = get_visible(run_id, user)
    if body.action == "accept":
        run["decision"] = "accepted"
        store.add_audit("Accepted a fix", run["repo"], login_of(user))
    elif body.action == "reject":
        run["decision"] = "rejected"
        run["rejectReason"] = body.reason
        run["rejectNote"] = body.note
        store.add_audit(f"Rejected a fix ({body.reason or 'no reason'})", run["repo"], login_of(user))
    elif body.action == "assign":
        run["assignee"] = body.assignee
        if run["status"] == "ready":
            run["status"] = "person"
            run["reason"] = run["reason"] or "Assigned for human review"
        store.add_audit(f"Assigned fix to {body.assignee}", run["repo"], login_of(user))
    else:
        raise HTTPException(400, "Unknown action")
    store.save()
    return public_run(run)


@router.post("/runs/{run_id}/conflict")
def resolve_conflict(run_id: str, body: DecisionBody, user=Depends(require_user)):
    run = get_visible(run_id, user)
    run["conflictResolution"] = {"text": body.note, "by": body.assignee or login_of(user), "at": now_iso()}
    store.add_audit("Resolved a merge conflict", run["repo"], login_of(user))
    store.save()
    return {"ok": True}


# ── memory ("Past fixes") ────────────────────────────────────────────────────

_memory_client = None


def _memory():
    global _memory_client
    from agents.execution_mode import is_patch_memory_enabled
    if not is_patch_memory_enabled():
        return None
    if _memory_client is None:
        try:
            from agents.chroma_memory import init_memory
            _memory_client = init_memory(os.environ.get("CHROMADB_PATH", "./chroma_data"))
        except Exception as e:
            logger.warning(f"[API] ChromaDB memory init failed: {e}")
            return None
    return _memory_client


def _lang(path: str) -> str:
    ext = os.path.splitext(path or "")[1].lower()
    return {".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".go": "Go", ".kt": "Kotlin", ".java": "Java"}.get(ext, "Python")


@router.get("/memory")
def memory(q: str = "", limit: int = 50):
    from agents.execution_mode import is_patch_memory_enabled
    if not is_patch_memory_enabled():
        return {
            "items": [],
            "total": 0,
            "available": False,
            "reason": "ChromaDB patch memory is disabled in hosted cloud mode to stay within Render's 512 MB memory limit.",
        }

    try:
        client = _memory()
        if client is None:
            return {"items": [], "total": 0, "available": False, "reason": "Patch memory unavailable."}
        from agents.chroma_memory import _get_collection

        col = _get_collection(client)
        if col is None:
            return {"items": [], "total": 0, "available": False, "reason": "Collection unavailable."}
        items = []
        if q.strip():
            res = col.query(query_texts=[q], n_results=min(limit, max(1, col.count() or 1)), include=["metadatas", "distances", "documents"])
            metas, dists = res["metadatas"][0], res["distances"][0]
            ids = res["ids"][0]
            sims = [round(100 / (1 + d)) for d in dists]
        else:
            res = col.get(include=["metadatas"], limit=limit)
            metas, ids, sims = res["metadatas"], res["ids"], [None] * len(res["ids"])
        reuse_counts: dict[str, int] = {}
        for r in store.runs:
            if r.get("memoryHit"):
                reuse_counts[r["rootCause"]] = reuse_counts.get(r["rootCause"], 0) + 1
        for i, m in enumerate(metas):
            patch = m.get("patch_diff", "")
            fpath = next((l[6:].strip() for l in patch.splitlines() if l.startswith("--- a/")), m.get("test_file", ""))
            key = m.get("commit_sha", ids[i])
            items.append(
                {
                    "id": ids[i],
                    "key": key,
                    "title": m.get("root_cause", "") or m.get("error_log", "")[:100],
                    "repo": m.get("repo", "") or "demo repository",
                    "language": _lang(fpath),
                    "similarity": sims[i],
                    "reuses": reuse_counts.get(m.get("root_cause", ""), 0),
                    "commit": (m.get("commit_sha", "") or "")[:7],
                    "date": m.get("date", ""),
                    "patch": patch,
                    "testFile": m.get("test_file", ""),
                    "reuse": store.memory_flags.get(key, True),
                }
            )
        items.sort(key=lambda x: (x["similarity"] or 0, x["date"]), reverse=True)
        return {"items": items, "total": col.count(), "available": True}
    except Exception as e:
        logger.warning(f"[API] memory unavailable: {e}")
        return {"items": [], "total": 0, "available": False, "error": str(e)}


class MemoryFlag(BaseModel):
    reuse: bool


@router.patch("/memory/{key}")
def memory_flag(key: str, body: MemoryFlag, user=Depends(require_user)):
    store.memory_flags[key] = body.reuse
    store.add_audit(("Enabled" if body.reuse else "Disabled") + " fix reuse", key[:12], login_of(user))
    store.save()
    return {"ok": True}


# ── settings ─────────────────────────────────────────────────────────────────

def _ollama_up() -> bool:
    try:
        import httpx

        r = httpx.get(os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"), timeout=1.5)
        return r.status_code < 500
    except Exception:
        return False


def model_status() -> dict:
    has_key = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    return {
        "gemini": {"connected": has_key, "detail": "API key found in .env" if has_key else "No API key in .env"},
        "gemma": {"connected": has_key, "detail": "Shares the Gemini API key" if has_key else "No API key in .env"},
        "ollama": {"connected": _ollama_up(), "detail": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")},
        "mock": {"connected": True, "detail": "Always available"},
    }


@router.get("/settings")
def get_settings(user=Depends(require_user)):
    s = dict(store.settings)
    s["repositories"] = [r for r in store.repos() if user is None or store.watched.get(r["name"]) == user.get("login")] if store.watched else store.repos()
    s["modelStatus"] = model_status()
    s["integrations"] = {
        "githubPat": bool(os.getenv("GITHUB_PAT")),
        "webhookSecret": bool(os.getenv("GITHUB_WEBHOOK_SECRET")),
        "webhookPath": "/webhook",
    }
    return s


@router.put("/settings")
def put_settings(body: dict, user=Depends(require_user)):
    allowed = {"repositories", "models", "safety", "team", "notifications"}
    for k, v in body.items():
        if k in allowed:
            store.settings[k] = v
    store.add_audit("Updated settings", "Workspace", login_of(user))
    store.save()
    return get_settings(user)


@router.post("/settings/models/{model_id}/test")
def test_model(model_id: str):
    status = model_status().get(model_id)
    if not status:
        raise HTTPException(404, "Unknown model")
    return status


# ── audit + health ───────────────────────────────────────────────────────────

@router.get("/audit")
def audit(q: str = "", limit: int = 200):
    ql = q.lower().strip()
    rows = [a for a in store.audit if not ql or ql in (a["action"] + a["scope"] + a["actor"]).lower()]
    return {"rows": rows[:limit], "total": len(store.audit)}


