"""
SQLite database for tracking blog pipeline state.
Tracks: discovered topics, issues, PRs, review iterations, and overall status.
"""
import aiosqlite
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async def initialize(self):
        """Create tables if they don't exist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS blog_topics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    target_keywords TEXT DEFAULT '[]',
                    search_volume TEXT DEFAULT 'medium',
                    outline TEXT DEFAULT '[]',
                    spheron_angle TEXT DEFAULT '',
                    status TEXT DEFAULT 'discovered',
                    issue_number INTEGER,
                    pr_number INTEGER,
                    planning_session_id TEXT,
                    review_score REAL,
                    review_iterations INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    completed_at TEXT,
                    metadata TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS review_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    blog_topic_id INTEGER NOT NULL,
                    pr_number INTEGER NOT NULL,
                    iteration INTEGER DEFAULT 1,
                    review_type TEXT DEFAULT 'editorial',
                    score REAL,
                    review_data TEXT DEFAULT '{}',
                    comment_ids TEXT DEFAULT '[]',
                    created_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (blog_topic_id) REFERENCES blog_topics(id)
                );

                CREATE TABLE IF NOT EXISTS pricing_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    blog_topic_id INTEGER,
                    pr_number INTEGER,
                    pricing_data TEXT DEFAULT '{}',
                    mismatches TEXT DEFAULT '[]',
                    checked_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (blog_topic_id) REFERENCES blog_topics(id)
                );

                CREATE TABLE IF NOT EXISTS agent_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_type TEXT NOT NULL,
                    blog_topic_id INTEGER,
                    status TEXT DEFAULT 'running',
                    started_at TEXT DEFAULT (datetime('now')),
                    finished_at TEXT,
                    result TEXT DEFAULT '{}',
                    error TEXT,
                    FOREIGN KEY (blog_topic_id) REFERENCES blog_topics(id)
                );

                CREATE TABLE IF NOT EXISTS agent_run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    phase TEXT DEFAULT 'general',
                    event_type TEXT,
                    event_data TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (run_id) REFERENCES agent_runs(id)
                );
            """)
            await db.commit()

            # Safe column migrations for existing DBs
            _migrations = [
                "ALTER TABLE agent_runs ADD COLUMN pid INTEGER",
                "ALTER TABLE agent_runs ADD COLUMN log_path TEXT",
                "ALTER TABLE agent_runs ADD COLUMN log_offset INTEGER DEFAULT 0",
                "ALTER TABLE agent_runs ADD COLUMN recovery_context TEXT DEFAULT '{}'",
            ]
            for sql in _migrations:
                try:
                    await db.execute(sql)
                    await db.commit()
                except Exception:
                    pass  # Column already exists

    # === Blog Topics ===

    async def create_topic(self, title: str, keywords: list = None, outline: list = None,
                           spheron_angle: str = "", search_volume: str = "medium") -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO blog_topics (title, target_keywords, outline, spheron_angle, search_volume)
                   VALUES (?, ?, ?, ?, ?)""",
                (title, json.dumps(keywords or []), json.dumps(outline or []),
                 spheron_angle, search_volume),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_topic(self, topic_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM blog_topics WHERE id = ?", (topic_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_topic_by_issue(self, issue_number: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM blog_topics WHERE issue_number = ?", (issue_number,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_topic_by_pr(self, pr_number: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM blog_topics WHERE pr_number = ?", (pr_number,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_topics(self, status: str = "", limit: int = 50) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if status:
                cursor = await db.execute(
                    "SELECT * FROM blog_topics WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM blog_topics ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def update_topic_status(self, topic_id: int, status: str, **kwargs):
        async with aiosqlite.connect(self.db_path) as db:
            sets = ["status = ?", "updated_at = datetime('now')"]
            values = [status]
            for key, val in kwargs.items():
                sets.append(f"{key} = ?")
                values.append(val)
            if status == "completed":
                sets.append("completed_at = datetime('now')")
            values.append(topic_id)
            await db.execute(
                f"UPDATE blog_topics SET {', '.join(sets)} WHERE id = ?",
                values,
            )
            await db.commit()

    async def topic_title_exists(self, title: str) -> bool:
        """Check if a similar topic already exists (fuzzy match)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM blog_topics WHERE LOWER(title) LIKE ?",
                (f"%{title.lower()[:50]}%",),
            )
            row = await cursor.fetchone()
            return row[0] > 0

    async def get_topic_by_title(self, title: str) -> dict | None:
        """Get a topic by exact title match (case-insensitive)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM blog_topics WHERE LOWER(title) = ? ORDER BY id DESC LIMIT 1",
                (title.lower(),),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_all_topic_titles(self) -> list[str]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT title FROM blog_topics")
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    # === Review Logs ===

    async def create_review_log(self, topic_id: int, pr_number: int, iteration: int,
                                review_type: str, score: float, review_data: dict,
                                comment_ids: list = None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO review_logs (blog_topic_id, pr_number, iteration, review_type,
                   score, review_data, comment_ids) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (topic_id, pr_number, iteration, review_type, score,
                 json.dumps(review_data), json.dumps(comment_ids or [])),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_review_logs(self, pr_number: int) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM review_logs WHERE pr_number = ? ORDER BY iteration ASC",
                (pr_number,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_latest_review_iteration(self, pr_number: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT MAX(iteration) FROM review_logs WHERE pr_number = ?",
                (pr_number,),
            )
            row = await cursor.fetchone()
            return row[0] or 0

    # === Pricing Checks ===

    async def create_pricing_check(self, topic_id: int, pr_number: int,
                                   pricing_data: dict, mismatches: list) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO pricing_checks (blog_topic_id, pr_number, pricing_data, mismatches)
                   VALUES (?, ?, ?, ?)""",
                (topic_id, pr_number, json.dumps(pricing_data), json.dumps(mismatches)),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_pricing_check_for_pr(self, pr_number: int) -> Optional[dict]:
        """Return the first pricing check record for this PR, or None if not yet checked."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM pricing_checks WHERE pr_number = ? ORDER BY checked_at ASC LIMIT 1",
                (pr_number,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    # === Agent Runs ===

    async def create_agent_run(self, agent_type: str, topic_id: int = None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO agent_runs (agent_type, blog_topic_id) VALUES (?, ?)",
                (agent_type, topic_id),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_running_reviews_for_pr(self, pr_number: int) -> list[dict]:
        """Get all running reviewer agent runs for a specific PR."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM agent_runs
                   WHERE agent_type = 'reviewer'
                     AND status = 'running'
                     AND json_extract(result, '$.pr_number') = ?
                   ORDER BY id ASC""",
                (pr_number,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def set_agent_run_pr(self, run_id: int, pr_number: int):
        """Store pr_number in result JSON immediately so it's visible while running."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE agent_runs SET result = ? WHERE id = ?",
                (json.dumps({"pr_number": pr_number}), run_id),
            )
            await db.commit()

    async def get_agent_run(self, run_id: int) -> dict | None:
        """Get a single agent run by ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def finish_agent_run(self, run_id: int, status: str = "completed",
                               result: dict = None, error: str = None) -> bool:
        """Finish an agent run. Merges result with existing result data (preserving
        pr_number etc. set earlier). Returns True if the run was updated, False if it
        was already in a terminal state (stopped/cancelled/completed)."""
        async with aiosqlite.connect(self.db_path) as db:
            # Read existing result to merge (preserves pr_number set by set_agent_run_pr)
            existing = {}
            cursor = await db.execute(
                "SELECT result FROM agent_runs WHERE id = ? AND status = 'running'",
                (run_id,),
            )
            row = await cursor.fetchone()
            if row and row[0]:
                try:
                    existing = json.loads(row[0])
                    if not isinstance(existing, dict):
                        existing = {}
                except (json.JSONDecodeError, TypeError):
                    existing = {}

            merged = {**existing, **(result or {})}

            # Only update if still running - prevents race where a stop/cancel
            # already set a terminal status and a background task overwrites it.
            cursor = await db.execute(
                """UPDATE agent_runs SET status = ?, finished_at = datetime('now'),
                   result = ?, error = ? WHERE id = ? AND status = 'running'""",
                (status, json.dumps(merged), error, run_id),
            )
            await db.commit()
            if cursor.rowcount == 0:
                logger.info(f"Run #{run_id}: finish_agent_run({status}) skipped - already in terminal state")
            return cursor.rowcount > 0

    async def update_agent_run_process(self, run_id: int, pid: int, log_path: str):
        """Store PID and log path after spawning Claude subprocess."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE agent_runs SET pid = ?, log_path = ? WHERE id = ?",
                (pid, log_path, run_id),
            )
            await db.commit()

    async def get_agent_run_pid(self, run_id: int) -> int | None:
        """Get the PID of a running agent subprocess."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT pid FROM agent_runs WHERE id = ?", (run_id,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def update_agent_run_log_offset(self, run_id: int, offset: int):
        """Persist current log byte offset for restart recovery."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE agent_runs SET log_offset = ? WHERE id = ?",
                (offset, run_id),
            )
            await db.commit()

    async def update_agent_run_recovery_context(self, run_id: int, context: dict):
        """Store recovery context so the run can be resumed after restart."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE agent_runs SET recovery_context = ? WHERE id = ?",
                (json.dumps(context), run_id),
            )
            await db.commit()

    async def get_running_agent_runs(self) -> list[dict]:
        """Get all agent runs still marked running (for startup recovery)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM agent_runs WHERE status = 'running' ORDER BY started_at ASC"
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def list_agent_runs(self, limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
        """List agent runs with pagination. Returns (runs, total_count)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            rows = await cursor.fetchall()
            count_cursor = await db.execute("SELECT COUNT(*) FROM agent_runs")
            count_row = await count_cursor.fetchone()
            total = count_row[0] if count_row else 0
            return [dict(r) for r in rows], total

    # === Agent Run Events (streaming logs) ===

    async def insert_run_event(self, run_id: int, phase: str, event_type: str, event_data: str) -> int:
        """Insert a single streaming event for an agent run."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO agent_run_events (run_id, phase, event_type, event_data) VALUES (?, ?, ?, ?)",
                (run_id, phase, event_type, event_data),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_run_events(self, run_id: int, since_id: int = 0, limit: int = 200) -> list[dict]:
        """Retrieve streaming events for a run, cursor-based (id > since_id)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM agent_run_events WHERE run_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (run_id, since_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # === Dashboard Stats ===

    async def get_pipeline_stats(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            stats = {}
            for status in ["discovered", "planning", "issue_created", "writing",
                           "pr_created", "reviewing", "ready", "completed"]:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM blog_topics WHERE status = ?", (status,)
                )
                row = await cursor.fetchone()
                stats[status] = row[0]

            cursor = await db.execute("SELECT COUNT(*) FROM blog_topics")
            row = await cursor.fetchone()
            stats["total"] = row[0]

            cursor = await db.execute(
                "SELECT AVG(review_score) FROM blog_topics WHERE review_score IS NOT NULL"
            )
            row = await cursor.fetchone()
            stats["avg_review_score"] = round(row[0], 1) if row[0] else 0

            return stats
