import asyncio
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# Load environment variables before other imports
load_dotenv(override=True)

import uvicorn
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.webhook import router as webhook_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Debug: check env vars on reload
for k, v in os.environ.items():
    if k.startswith("GITHUB_"):
        logger.info(f"[Debug] Env: {k} = {'*' * len(v)}")

from agents.orchestrator import graph

from api.model_keys import router as model_keys_router, run_credentials
from agents.llm_client import personal_credentials
from api.main_state import pipeline_status, reset_agents
from api.recorder import apply_node, finish_run, run_log_handler, start_run
from api.auth import auth_router, oauth_configured, token_for_login, token_for_repo
from api.routes import router as api_router, github_token
from api.store import store
from agents.execution_mode import get_execution_mode, log_memory_usage, trigger_garbage_collection

# ── Async consumer: drains the webhook queue → runs LangGraph ────────────────

AGENT_FOR_NODE = {
    "analyzing": ("analyst", "bisect", "BISECTING"),
    "bisecting": ("bisect", "patch_generator", "PATCHING"),
    "patching": ("patch_generator", "validator", "VALIDATING"),
    "validating": ("validator", "pr_agent", "GENERATING TEST"),
}


async def process_events(app: FastAPI):
    """Pulls events off the queue and streams each through the LangGraph workflow,
    recording every step into the run store so the dashboard is live.
    Strictly limits concurrency to 1 on free-tier instances."""
    event_queue = app.state.event_queue
    logger.info("[Consumer] Event processor started — waiting for events (Concurrency limit: 1)")
    logging.getLogger().addHandler(run_log_handler)
    while True:
        event = await event_queue.get()
        app.state.is_repair_active = True
        run = store.get_run(event.get("run_id", "")) or store.create_run(event)
        log_memory_usage(f"Start repair run {run['id']} ({event.get('repo_full_name')})")

        reset_agents()
        pipeline_status.update(
            pr_url=None, error=None, current_run_id=run["id"], current_state="ANALYZING",
            last_event={
                "repo": event.get("repo_full_name"),
                "sha": event.get("commit_sha"),
                "triggered_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        pipeline_status["agents"]["analyst"] = "running"
        run_log_handler.run = run
        start_run(run)

        final = {}
        error = None
        credential_context = None
        token_ctx = None
        try:
            async with app.state.repair_semaphore:
                initial_state = {
                    "event": event,
                    "repo_full_name": event.get("repo_full_name", ""),
                    "commit_sha": event.get("commit_sha", ""),
                    "failure_log": event.get("failure_log", ""),
                    "failing_test": "unknown_test",
                    "retry_count": 0,
                }
                login = run.get("owner") if oauth_configured() else None
                if run.get("source") == "webhook" and store.watched.get(run["repo"]) != login:
                    login = None
                credential_context = personal_credentials.set(run_credentials(login))
                tok = token_for_login(run.get("owner", "")) or token_for_repo(run["repo"])
                token_ctx = github_token(tok)
                token_ctx.__enter__()
                async for step in graph.astream(initial_state):
                    for node_name, state in step.items():
                        final = state
                        log_memory_usage(f"Node '{node_name}' finished")
                        apply_node(run, node_name, state)
                        if node_name in AGENT_FOR_NODE:
                            done, nxt, label = AGENT_FOR_NODE[node_name]
                            pipeline_status["agents"][done] = "done"
                            pipeline_status["agents"][nxt] = "running"
                            pipeline_status["current_state"] = label
                        elif node_name == "generating_test":
                            pipeline_status["current_state"] = "CREATING PR"
                        elif node_name == "creating_pr":
                            pipeline_status["agents"]["pr_agent"] = "done"
                        elif node_name == "done":
                            pipeline_status["current_state"] = "DONE"
                        elif node_name == "escalated":
                            pipeline_status["current_state"] = "ESCALATED"
                            pipeline_status["error"] = final.get("error")
        except Exception as e:
            error = str(e)
            logger.error(f"[Consumer] Unhandled error: {error}")
        finally:
            if credential_context is not None:
                personal_credentials.reset(credential_context)
            if token_ctx is not None:
                token_ctx.__exit__(None, None, None)
            run_log_handler.run = None
            try:
                finish_run(run, final, error)
            except Exception as e:  # never let bookkeeping kill the consumer
                logger.error(f"[Consumer] finish_run failed: {e}")
            pipeline_status["pr_url"] = run.get("prUrl")
            pipeline_status["current_state"] = {"ready": "DONE", "working": "DONE"}.get(run["status"], "ESCALATED")
            pipeline_status["error"] = run.get("error")
            app.state.is_repair_active = False
            trigger_garbage_collection()
            log_memory_usage(f"End repair run {run['id']}")
            event_queue.task_done()


# ── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the background consumer on startup, cancel on shutdown."""
    # Create the queue and concurrency limiter inside the running event loop
    app.state.event_queue = asyncio.Queue(maxsize=5)
    app.state.repair_semaphore = asyncio.Semaphore(1)
    app.state.is_repair_active = False

    log_memory_usage(f"Startup | Mode: {get_execution_mode()} | Max concurrent: 1")

    task = asyncio.create_task(process_events(app))
    logger.info("[Lifespan] Background event processor launched (concurrency=1)")
    yield
    task.cancel()
    logger.info("[Lifespan] Background event processor cancelled")


app = FastAPI(title="ARCANE API", lifespan=lifespan)

# ── CORS — allow the React dev server (port 5173) to talk to FastAPI ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8443", "http://127.0.0.1:8443"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook_router)
app.include_router(auth_router)
app.include_router(model_keys_router)
app.include_router(api_router)


@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/api/status")
async def get_status():
    """Returns the current LangGraph pipeline telemetry for the dashboard."""
    return pipeline_status

if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
