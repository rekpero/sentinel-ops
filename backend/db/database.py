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
            """)
            await db.commit()

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

    # === Agent Runs ===

    async def create_agent_run(self, agent_type: str, topic_id: int = None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO agent_runs (agent_type, blog_topic_id) VALUES (?, ?)",
                (agent_type, topic_id),
            )
            await db.commit()
            return cursor.lastrowid

    async def finish_agent_run(self, run_id: int, status: str = "completed",
                               result: dict = None, error: str = None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE agent_runs SET status = ?, finished_at = datetime('now'),
                   result = ?, error = ? WHERE id = ?""",
                (status, json.dumps(result or {}), error, run_id),
            )
            await db.commit()

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
