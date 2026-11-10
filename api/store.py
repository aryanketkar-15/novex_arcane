"""ARCANE run store — persistent run history, settings, audit log.

Everything the dashboard shows comes from here. State is kept in memory and
mirrored to ``data/arcane_store.json`` so history survives restarts.
"""
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STORE_FILE = os.path.join(DATA_DIR, "arcane_store.json")

MAX_LOG_LINES = 600

STEP_TEMPLATE = [
    ("failed", "Build failed", "Waiting for failure details."),
    ("bisect", "Found the bad commit", "Git bisect has not run yet."),
    ("patch", "Wrote a fix", "No patch generated yet."),
    ("related", "Checked related code", "Related code has not been checked yet."),
    ("sandbox", "Tested in a sandbox", "Sandbox has not run yet."),
    ("ready", "Ready for review", "Waiting for earlier steps."),
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "workspace": "Team Mosambi",
    "repositories": [],  # filled lazily from runs + env
    "models": [
        {"id": "gemini", "name": os.getenv("GEMINI_MODEL", "gemini-3.5-flash"), "role": "Primary"},
        {"id": "gemma", "name": os.getenv("GEMMA_MODEL", "gemma-4-31b-it"), "role": "Fallback 1"},
        {"id": "ollama", "name": "local llama3:8b", "role": "Fallback 2"},
        {"id": "mock", "name": "offline mock", "role": "Fallback 3"},
    ],
    "safety": {
        "minConfidence": 85,
        "escalateBelow": 60,
        "protectedBranches": ["main", "release/*"],
        "protectedFiles": "migrations/**\n.github/workflows/**\ninfra/production/**",
        "network": "Off",
        "cpu": 2,
        "memoryGb": 4,
        "runtimeMin": 12,
    },
    "team": [
        {"name": "Rishi", "email": "rishi@bahadur.dev", "role": "Admin", "lead": "GitHub Integration"},
        {"name": "Aryan", "email": "mightzaryan@gmail.com", "role": "Admin", "lead": "AST + Memory"},
        {"name": "Ajinkya", "email": "ajinkya@bahadur.dev", "role": "Reviewer", "lead": "ML + Infra"},
        {"name": "Ajaya", "email": "ajaya@bahadur.dev", "role": "Reviewer", "lead": "Sandbox + Testing"},
    ],
    "notifications": {
        "events": {
            "ready": {"on": True, "email": True, "slack": True},
            "person": {"on": True, "email": True, "slack": True},
            "failed": {"on": True, "email": True, "slack": True},
            "started": {"on": False, "email": False, "slack": False},
            "pr": {"on": True, "email": True, "slack": False},
        },
        "email": "",
        "slack": "#build-repairs",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fresh_steps() -> list[dict]:
    return [
        {"key": k, "title": t, "body": b, "evidence": "", "status": "pending"}
        for k, t, b in STEP_TEMPLATE
    ]


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.runs: list[dict] = []
        self.audit: list[dict] = []
        self.settings: dict = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.memory_flags: dict[str, bool] = {}
        self.watched: dict[str, str] = {}  # repo -> login of the user watching it
        self._load()

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with open(STORE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.runs = data.get("runs", [])
            self.audit = data.get("audit", [])
            self.memory_flags = data.get("memory_flags", {})
            self.watched = data.get("watched", {})
            saved = data.get("settings", {})
            if saved.get("workspace") == "Team Bahadur":
                saved["workspace"] = DEFAULT_SETTINGS["workspace"]
            for k, v in saved.items():
                self.settings[k] = v
        except Exception:
            pass

        # If JSON was missing/empty (e.g. fresh ephemeral container), attempt restore from SQLite DB
        if not self.runs:
            try:
                from api.db import db_load_all_runs
                db_runs = db_load_all_runs()
                if db_runs:
                    self.runs = db_runs
            except Exception:
                pass

        # Runs interrupted by a restart can never finish — mark them.
        for r in self.runs:
            if r.get("status") == "working":
                r["status"] = "failed"
                r["error"] = "Interrupted by a server restart."
                r["finishedAt"] = now_iso()

    def save(self) -> None:
        with self._lock:
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                tmp = STORE_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "runs": self.runs,
                            "audit": self.audit[-1000:],
                            "settings": self.settings,
                            "memory_flags": self.memory_flags,
                            "watched": self.watched,
                        },
                        f,
                        default=str,
                    )
                os.replace(tmp, STORE_FILE)
            except Exception:
                pass

            # Also mirror latest runs to SQLite DB for durability
            try:
                from api.db import db_save_run
                for r in self.runs[:50]:
                    db_save_run(r)
            except Exception:
                pass

    # ── runs ─────────────────────────────────────────────────────────────
    def create_run(self, event: dict) -> dict:
        with self._lock:
            run = {
                "id": event.get("run_id") or uuid.uuid4().hex[:10],
                "repo": event.get("repo_full_name", ""),
                "branch": event.get("branch", "main"),
                "sha": event.get("commit_sha", "") or "",
                "commitMessage": event.get("commit_message", ""),
                "author": event.get("author", ""),
                "source": event.get("source", "webhook"),
                "owner": event.get("owner", ""),
                "hint": event.get("hint", ""),
                "workflowRun": event.get("workflow_run", ""),
                "failureLog": event.get("failure_log", ""),
                "status": "working",  # working | ready | person | failed
                "stage": "QUEUED",
                "title": "Analyzing build failure",
                "failingTest": "",
                "failingFile": "",
                "failingLine": 0,
                "suspectedFunction": "",
                "rootCause": "",
                "bisectReport": "",
                "patchDiff": "",
                "regressionTest": "",
                "cascadeReport": "",
                "confidence": None,
                "testsPassed": None,
                "executionMode": event.get("execution_mode", "auto"),
                "validationStatus": None,
                "testVerification": None,
                "isDraftPr": False,
                "autoApproved": False,
                "model": "",
                "attempts": 0,
                "prUrl": None,
                "error": None,
                "memoryHit": False,
                "conflict": None,
                "validatorSummary": "",
                "agentsUsed": [],
                "steps": _fresh_steps(),
                "logs": [],
                "decision": "none",  # none | accepted | rejected
                "rejectReason": "",
                "assignee": "",
                "reason": "",
                "startedAt": now_iso(),
                "finishedAt": None,
                "queuedAt": now_iso(),
            }
            self.runs.insert(0, run)
            self.runs = self.runs[:300]
            self.save()
            return run

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._lock:
            for r in self.runs:
                if r["id"] == run_id:
                    return r
        return None

    def log(self, run: dict, level: str, msg: str) -> None:
        with self._lock:
            run["logs"].append({"t": now_iso(), "level": level, "msg": msg[:2000]})
            if len(run["logs"]) > MAX_LOG_LINES:
                del run["logs"][: len(run["logs"]) - MAX_LOG_LINES]

    def set_step(self, run: dict, key: str, status: str, body: str = "", evidence: str = "") -> None:
        with self._lock:
            for s in run["steps"]:
                if s["key"] == key:
                    s["status"] = status
                    if body:
                        s["body"] = body
                    if evidence:
                        s["evidence"] = evidence
                    return

    def add_audit(self, action: str, scope: str, actor: str = "ARCANE") -> None:
        with self._lock:
            self.audit.insert(0, {"t": now_iso(), "action": action, "scope": scope, "actor": actor})
            self.save()

    # ── derived data ─────────────────────────────────────────────────────
    def repos(self) -> list[dict]:
        with self._lock:
            saved = {r["name"]: r for r in self.settings.get("repositories", [])}
            seen = list(saved.keys())
            for r in self.runs:
                if r["repo"] and r["repo"] not in saved:
                    saved[r["repo"]] = default_repo(r["repo"])
                    seen.append(r["repo"])
            default = os.getenv("ARCANE_DEFAULT_REPO", "")
            if default and default not in saved:
                saved[default] = default_repo(default)
            return list(saved.values())


def default_repo(name: str) -> dict:
    return {
        "name": name,
        "testCommand": "pytest -q",
        "language": "Python",
        "timeoutMin": 12,
        "autoPr": True,
    }


store = Store()
