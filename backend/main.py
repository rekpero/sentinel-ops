"""
Sentinel - FastAPI backend.
Provides API endpoints for the dashboard and orchestrates the discovery + review agents.
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

from backend import config
from backend.db.database import Database
from backend.services.github_service import GitHubService
from backend.services.claude_service import ClaudeService
from backend.services.pricing_service import PricingService
from backend.services.swarmops_service import SwarmOpsService
from backend.agents.discovery import DiscoveryAgent
from backend.agents.reviewer import ReviewAgent
from backend.scheduler.cron import setup_scheduler, start_scheduler, stop_scheduler

# === Logging ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# === Globals ===
db: Database = None
github: GitHubService = None
claude: ClaudeService = None
pricing: PricingService = None
swarmops: SwarmOpsService = None
discovery_agent: DiscoveryAgent = None
review_agent: ReviewAgent = None


async def _recover_running_agents():
    """
    On startup, check all agent runs still marked 'running'.
    - If the Claude subprocess (PID) is still alive: reattach and continue processing.
    - If the PID is dead: mark as error (next poll will retry the review).
    """
    running = await db.get_running_agent_runs()
    if not running:
        return

    logger.info(f"Found {len(running)} agent run(s) in 'running' state at startup")

    for run in running:
        run_id = run["id"]
        pid = run.get("pid")
        log_path = run.get("log_path")
        log_offset = run.get("log_offset") or 0

        if not pid or not log_path:
            await db.finish_agent_run(
                run_id, "error",
                error="Orphaned: server restarted (no PID recorded, cannot recover)",
            )
            logger.warning(f"Run {run_id}: no PID/log recorded - marked as error")
            continue

        try:
            os.kill(pid, 0)
            pid_alive = True
        except (OSError, ProcessLookupError):
            pid_alive = False

        if not pid_alive:
            await db.finish_agent_run(
                run_id, "error",
                error=f"Orphaned: Claude process (PID {pid}) died during server restart",
            )
            logger.warning(f"Run {run_id}: PID {pid} is dead - marked as error")
            continue

        logger.info(f"Run {run_id}: PID {pid} still alive - reattaching from offset={log_offset}")
        ctx = {}
        try:
            ctx = json.loads(run.get("recovery_context", "{}") or "{}")
        except Exception:
            pass

        asyncio.create_task(_reattach_and_finish(run_id, pid, log_path, log_offset, ctx))


async def _reattach_and_finish(
    run_id: int, pid: int, log_path: str, log_offset: int, ctx: dict
):
    """Tail the surviving Claude process log and run post-result logic when it finishes."""
    try:
        result = await claude.reattach_run(
            run_id=run_id,
            pid=pid,
            log_path=log_path,
            log_offset=log_offset,
            phase=ctx.get("phase", "review"),
        )

        if ctx.get("type") == "review" and review_agent is not None:
            pr_number = ctx.get("pr_number")
            topic_id = ctx.get("topic_id")
            iteration = ctx.get("iteration", 1)
            worktree_path = ctx.get("worktree_path", "")
            head_sha = ctx.get("head_sha", "")

            if pr_number and topic_id:
                # Try to re-read blog content for pricing check
                blog_content = ""
                try:
                    files = await review_agent.github.get_pr_files(pr_number)
                    blog_files = [
                        f for f in files
                        if any(ext in f.get("filename", "").lower() for ext in [".md", ".mdx"])
                        and "blog" in f.get("filename", "").lower()
                    ]
                    if blog_files and worktree_path:
                        primary = max(blog_files, key=lambda f: f.get("additions", 0))
                        fpath = Path(worktree_path) / primary["filename"]
                        if fpath.exists():
                            blog_content = fpath.read_text(encoding="utf-8", errors="replace")
                except Exception as e:
                    logger.warning(f"Recovery run {run_id}: could not re-read blog content: {e}")

                is_ready = await review_agent._process_review_result(
                    pr_number=pr_number,
                    topic_id=topic_id,
                    iteration=iteration,
                    head_sha=head_sha,
                    blog_content=blog_content,
                    result=result,
                )
                await db.finish_agent_run(
                    run_id,
                    "completed",
                    {"pr_number": pr_number, "recovered": True, "ready": is_ready},
                )
                return

        # Generic finish for non-review runs
        await db.finish_agent_run(
            run_id,
            "completed" if result.get("success") else "error",
            {"recovered": True} if result.get("success") else None,
            error=result.get("error") if not result.get("success") else None,
        )

    except Exception as e:
        logger.error(f"Recovery failed for run {run_id}: {e}", exc_info=True)
        await db.finish_agent_run(run_id, "error", error=f"Recovery error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    global db, github, claude, pricing, swarmops, discovery_agent, review_agent

    # Initialize services
    db = Database(str(config.DB_PATH))
    await db.initialize()
    logger.info(f"Database initialized at {config.DB_PATH}")

    github = GitHubService(config.GITHUB_TOKEN, config.GITHUB_REPO)
    claude = ClaudeService(
        claude_cmd=config.CLAUDE_CMD,
        setup_token=config.CLAUDE_SETUP_TOKEN,
        workdir=str(config.WORKDIR),
        db=db,
    )
    pricing = PricingService(config.SPHERON_PRICING_API)
    swarmops = SwarmOpsService(config.SWARMOPS_URL, config.SWARMOPS_API_KEY)

    # Initialize agents
    discovery_agent = DiscoveryAgent(github, claude, swarmops, db)
    review_agent = ReviewAgent(github, claude, pricing, db)

    # Ensure workspace directories exist
    config.WORKDIR.mkdir(parents=True, exist_ok=True)
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Recover any runs that were active when the server last stopped
    await _recover_running_agents()

    # Setup and start scheduler
    setup_scheduler(
        discovery_callback=run_discovery,
        reviewer_callback=run_review_poll,
    )
    start_scheduler()

    logger.info("Sentinel started")
    yield

    # Shutdown
    stop_scheduler()
    await github.close()
    await pricing.close()
    await swarmops.close()
    logger.info("Sentinel stopped")


app = FastAPI(
    title="Sentinel",
    description="Blog pipeline guardian - automated discovery, writing, and review",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# === Scheduler callbacks ===

async def run_discovery():
    """Scheduled callback for blog discovery."""
    logger.info("[cron:discovery] Starting scheduled discovery run...")
    try:
        await discovery_agent.run()
        logger.info("[cron:discovery] Discovery run completed")
    except Exception as e:
        logger.error(f"[cron:discovery] Discovery run failed: {e}", exc_info=True)


async def run_review_poll():
    """Scheduled callback for PR review polling."""
    logger.info("[cron:review-poll] Starting scheduled PR review poll...")
    try:
        await review_agent.poll_prs()
        logger.info("[cron:review-poll] Review poll completed")
    except Exception as e:
        logger.error(f"[cron:review-poll] Review poll failed: {e}", exc_info=True)


# === Pydantic Models ===

class TopicCreate(BaseModel):
    title: str
    keywords: list[str] = []
    outline: list[str] = []
    spheron_angle: str = ""


class TriggerResponse(BaseModel):
    message: str
    success: bool


# === API Routes ===

@app.get("/api/health")
async def health():
    return {"status": "ok", "repo": config.GITHUB_REPO}


# --- Dashboard Stats ---

@app.get("/api/stats")
async def get_stats():
    """Get pipeline statistics for the dashboard."""
    stats = await db.get_pipeline_stats()
    return stats


# --- Blog Topics ---

@app.get("/api/topics")
async def list_topics(status: str = "", limit: int = 50):
    """List blog topics with optional status filter."""
    topics = await db.list_topics(status=status, limit=limit)
    # Parse JSON fields
    for t in topics:
        for field in ["target_keywords", "outline", "metadata"]:
            if isinstance(t.get(field), str):
                try:
                    t[field] = json.loads(t[field])
                except (json.JSONDecodeError, TypeError):
                    pass
    return topics


@app.get("/api/topics/{topic_id}")
async def get_topic(topic_id: int):
    """Get a single topic."""
    topic = await db.get_topic(topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    for field in ["target_keywords", "outline", "metadata"]:
        if isinstance(topic.get(field), str):
            try:
                topic[field] = json.loads(topic[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return topic


@app.post("/api/topics")
async def create_topic(topic: TopicCreate):
    """Manually create a blog topic."""
    topic_id = await db.create_topic(
        title=topic.title,
        keywords=topic.keywords,
        outline=topic.outline,
        spheron_angle=topic.spheron_angle,
    )
    return {"id": topic_id, "status": "discovered"}


# --- Reviews ---

@app.get("/api/reviews/{pr_number}")
async def get_reviews(pr_number: int):
    """Get review logs for a PR."""
    reviews = await db.get_review_logs(pr_number)
    for r in reviews:
        for field in ["review_data", "comment_ids"]:
            if isinstance(r.get(field), str):
                try:
                    r[field] = json.loads(r[field])
                except (json.JSONDecodeError, TypeError):
                    pass
    return reviews


# --- Pricing ---

@app.get("/api/pricing")
async def get_current_pricing():
    """Get current Spheron GPU pricing from the API."""
    try:
        summary = await pricing.get_pricing_summary()
        return {"pricing": summary, "source": config.SPHERON_PRICING_API}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Manual Triggers ---

@app.post("/api/trigger/discovery")
async def trigger_discovery():
    """Manually trigger blog topic discovery."""
    asyncio.create_task(run_discovery())
    return TriggerResponse(message="Discovery started", success=True)


@app.post("/api/trigger/review/{pr_number}")
async def trigger_review(pr_number: int):
    """Manually trigger review for a specific PR. Returns run_id for live log streaming."""
    run_id = await db.create_agent_run("reviewer", None)

    async def _review():
        try:
            await review_agent.ensure_repo_cloned()
            await review_agent.review_pr(pr_number, run_id=run_id)
            await db.finish_agent_run(run_id, "completed", {"pr_number": pr_number})
        except Exception as e:
            logger.error(f"Manual review failed: {e}")
            await db.finish_agent_run(run_id, "error", error=str(e))

    asyncio.create_task(_review())
    return {"message": f"Review started for PR #{pr_number}", "success": True, "run_id": run_id}


@app.post("/api/trigger/review-poll")
async def trigger_review_poll():
    """Manually trigger PR review polling."""
    asyncio.create_task(run_review_poll())
    return TriggerResponse(message="Review poll started", success=True)


# --- GitHub PRs ---

@app.get("/api/prs")
async def list_open_prs():
    """List open PRs with blog label."""
    try:
        prs = await github.list_prs(state="open", labels=config.GITHUB_BLOG_LABEL)
        return [
            {
                "number": pr.get("number"),
                "title": pr.get("title"),
                "state": pr.get("state"),
                "created_at": pr.get("created_at"),
                "updated_at": pr.get("updated_at"),
                "html_url": pr.get("html_url")
                    or pr.get("pull_request", {}).get("html_url", ""),
                "user": pr.get("user", {}).get("login", ""),
            }
            for pr in prs
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Agent Runs ---

@app.get("/api/runs")
async def list_agent_runs(limit: int = 20, offset: int = 0):
    """List recent agent runs with pagination."""
    async with __import__("aiosqlite").connect(str(config.DB_PATH)) as conn:
        conn.row_factory = __import__("aiosqlite").Row
        cursor = await conn.execute(
            "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        count_cursor = await conn.execute("SELECT COUNT(*) FROM agent_runs")
        count_row = await count_cursor.fetchone()
        total = count_row[0] if count_row else 0
        return {"runs": [dict(r) for r in rows], "total": total}


# --- Agent Run Live Logs ---

@app.get("/api/runs/{run_id}/logs")
async def get_run_logs(run_id: int, since: int = Query(0)):
    """
    Get streaming log events for a specific agent run.
    Use ?since={id} for cursor-based pagination (returns events with id > since).
    Poll this endpoint every 2-3 seconds while the run is active.
    """
    run_events = await db.get_run_events(run_id, since_id=since, limit=200)
    # Also return the run status so the frontend knows when to stop polling
    async with __import__("aiosqlite").connect(str(config.DB_PATH)) as conn:
        conn.row_factory = __import__("aiosqlite").Row
        cursor = await conn.execute(
            "SELECT status, finished_at FROM agent_runs WHERE id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        run_status = dict(row) if row else {"status": "unknown", "finished_at": None}
    return {"events": run_events, "run_status": run_status["status"], "finished_at": run_status["finished_at"]}


# --- Serve React Frontend ---

FRONTEND_DIR = Path(__file__).parent.parent / "frontend" / "dist"

if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve the React SPA for any non-API route."""
        file_path = FRONTEND_DIR / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(FRONTEND_DIR / "index.html"))
