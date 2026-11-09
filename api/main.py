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

# ── Shared pipeline state — LangGraph updates this at each node transition ──
pipeline_status = {
    "current_state": "IDLE",
    "last_event": None,
    "agents": {
        "analyst":         "idle",
        "bisect":          "idle",
        "patch_generator": "idle",
        "validator":       "idle",
        "pr_agent":        "idle",
    },
    "pr_url":  None,
    "error":   None,
}

# ── Async consumer: drains the webhook queue → runs LangGraph ────────────────

async def process_events(event_queue: asyncio.Queue):
    """
    Long-running task that pulls events from the webhook queue
    and feeds each one into the compiled LangGraph workflow.
    """
    logger.info("[Consumer] Event processor started — waiting for webhook events")
    while True:
        event = await event_queue.get()
        logger.info(
            f"[Consumer] Processing event for "
            f"{event.get('repo_full_name')} ({event.get('commit_sha', '?')[:7]})"
        )

        # ── Reset all agent statuses for this fresh run ──
        pipeline_status["agents"] = {
            "analyst":         "idle",
            "bisect":          "idle",
            "patch_generator": "idle",
            "validator":       "idle",
            "pr_agent":        "idle",
        }
        pipeline_status["pr_url"] = None
        pipeline_status["error"] = None

        # Stamp last_event so the dashboard can show it
        pipeline_status["last_event"] = {
            "repo":         event.get("repo_full_name"),
            "sha":          event.get("commit_sha"),
            "triggered_at": datetime.now(timezone.utc).isoformat(),
        }
        pipeline_status["current_state"] = "ANALYZING"
        pipeline_status["agents"]["analyst"] = "running"

        try:
            # Build initial state from webhook payload
            initial_state = {
                "repo_full_name": event.get("repo_full_name", ""),
                "commit_sha":     event.get("commit_sha", ""),
                "failure_log":    event.get("failure_log", ""),
                "patch_diff":     event.get("failure_log", "# placeholder patch"),
                "failing_test":   "unknown_test",
                "retry_count":    0,
            }

            # Stream through graph — each yielded step updates the dashboard live
            result = initial_state
            async for s in graph.astream(initial_state):
                for node_name, state in s.items():
                    result = state
                    logger.info(f"[Consumer] 🔄 Node completed: {node_name}")

                    # ── Map LangGraph node names → dashboard agent statuses ──
                    if node_name == "analyzing":
                        pipeline_status["agents"]["analyst"] = "done"
                        pipeline_status["current_state"] = "BISECTING"
                        pipeline_status["agents"]["bisect"] = "running"

                    elif node_name == "bisecting":
                        pipeline_status["agents"]["bisect"] = "done"
                        pipeline_status["current_state"] = "PATCHING"
                        pipeline_status["agents"]["patch_generator"] = "running"

                    elif node_name == "patching":
                        pipeline_status["agents"]["patch_generator"] = "done"
                        pipeline_status["current_state"] = "VALIDATING"
                        pipeline_status["agents"]["validator"] = "running"

                    elif node_name in ("propagating", "conflict_checking"):
                        # Internal nodes — keep validator as running
                        pass

                    elif node_name == "validating":
                        pipeline_status["agents"]["validator"] = "done"
                        pipeline_status["current_state"] = "GENERATING TEST"
                        pipeline_status["agents"]["pr_agent"] = "running"

                    elif node_name == "generating_test":
                        pipeline_status["current_state"] = "CREATING PR"

                    elif node_name == "creating_pr":
                        pipeline_status["agents"]["pr_agent"] = "done"

                    elif node_name == "done":
                        pipeline_status["current_state"] = "DONE"

                    elif node_name == "escalated":
                        pipeline_status["current_state"] = "ESCALATED"
                        pipeline_status["error"] = result.get("error", "Escalated after max retries")

            if result.get("pr_url"):
                pipeline_status["pr_url"] = result["pr_url"]
                pipeline_status["current_state"] = "DONE"
                pipeline_status["agents"]["pr_agent"] = "done"
                logger.info(f"[Consumer] ✅ PR created: {result['pr_url']}")
            elif result.get("error"):
                pipeline_status["current_state"] = "ESCALATED"
                pipeline_status["error"] = result["error"]
                logger.warning(f"[Consumer] ⚠️  Workflow ended with error: {result['error']}")
            else:
                pipeline_status["current_state"] = "DONE"
                logger.info(f"[Consumer] Workflow completed.")

        except Exception as e:
            err_str = str(e)
            logger.error(f"[Consumer] Unhandled error processing event: {err_str}")
            # If it's a 422 duplicate PR error, treat as partial success
            if "422" in err_str and "pull request already exists" in err_str.lower():
                pipeline_status["current_state"] = "DONE"
                pipeline_status["agents"]["pr_agent"] = "done"
                pipeline_status["error"] = None
                logger.info("[Consumer] PR already existed — treating as success.")
            else:
                pipeline_status["current_state"] = "ESCALATED"
                pipeline_status["error"] = err_str
        finally:
            event_queue.task_done()


# ── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the background consumer on startup, cancel on shutdown."""
    # Create the queue inside the running event loop
    app.state.event_queue = asyncio.Queue(maxsize=10)
    
    task = asyncio.create_task(process_events(app.state.event_queue))
    logger.info("[Lifespan] Background event processor launched")
    yield
    task.cancel()
    logger.info("[Lifespan] Background event processor cancelled")


app = FastAPI(title="ARCANE API", lifespan=lifespan)

# ── CORS — allow the React dev server (port 5173) to talk to FastAPI ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        os.getenv("FRONTEND_URL", "").rstrip("/"),
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook_router)


@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/api/status")
async def get_status():
    """Returns the current LangGraph pipeline telemetry for the dashboard."""
    return pipeline_status

if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
