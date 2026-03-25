"""
Blog Discovery Agent - searches for trending SEO topics,
validates against existing blogs, triggers SwarmOps for planning and issue creation,
and kicks off the writing agent.

SwarmOps integration flow:
1. POST /api/planning with { workspace_id, message }
2. Poll GET /api/planning/{id} until generating==false
3. POST /api/planning/{id}/create-issue -> get issue_number
4. Comment trigger mention on the issue via GitHub API
"""
import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from backend.services.github_service import GitHubService
from backend.services.claude_service import ClaudeService
from backend.services.swarmops_service import SwarmOpsService
from backend.db.database import Database
from backend import config

logger = logging.getLogger(__name__)

FOCUS_AREAS = """
- GPU cloud infrastructure and pricing comparisons
- New LLM deployments (DeepSeek, Llama, Qwen, Gemma, Mistral, etc.)
- New AI frameworks and tools for GPU workloads
- Multi-model research and how to use/deploy them
- Price comparisons with competitors (RunPod, Vast.ai, Lambda, CoreWeave)
- AI agent infrastructure and deployment
- Fine-tuning guides and best practices
- Inference optimization and serving
- GPU memory requirements and VRAM calculators
- Any trending AI/ML topic that drives GPU usage
"""


class DiscoveryAgent:
    def __init__(
        self,
        github: GitHubService,
        claude: ClaudeService,
        swarmops: SwarmOpsService,
        db: Database,
    ):
        self.github = github
        self.claude = claude
        self.swarmops = swarmops
        self.db = db
        self._workspace_id: Optional[str] = None
        self._run_id: Optional[int] = None

    async def _emit(self, phase: str, message: str, data: dict = None):
        """Emit a pipeline event visible in the dashboard log viewer."""
        if self._run_id is None:
            return
        event_payload = {"type": "pipeline", "phase": phase, "message": message}
        if data:
            event_payload["data"] = data
        try:
            await self.db.insert_run_event(
                run_id=self._run_id,
                phase=phase,
                event_type="pipeline",
                event_data=json.dumps(event_payload),
            )
        except Exception as e:
            logger.warning(f"Failed to emit pipeline event: {e}")

    async def _get_workspace_id(self) -> Optional[str]:
        """Get and cache the SwarmOps workspace ID for our repo."""
        if self._workspace_id:
            return self._workspace_id
        self._workspace_id = await self.swarmops.get_workspace_id(config.GITHUB_REPO)
        return self._workspace_id

    async def get_existing_blog_titles(self) -> list[str]:
        """Get existing blog titles from both the repo and our database."""
        titles = []

        # From our database
        db_titles = await self.db.get_all_topic_titles()
        titles.extend(db_titles)

        # From the repo - try to list blog directory
        try:
            repo_path = config.REPO_CLONE_DIR
            if repo_path.exists():
                # Look for blog content files (MDX/MD files in common blog dirs)
                result = subprocess.run(
                    ["find", str(repo_path), "-path", "*/blog*", "-name", "*.md*",
                     "-not", "-path", "*/node_modules/*"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split("\n"):
                        if line:
                            fname = Path(line).stem
                            titles.append(fname.replace("-", " ").title())
        except Exception as e:
            logger.warning(f"Could not scan repo for existing blogs: {e}")

        return titles

    @staticmethod
    def _extract_json_array(text: str) -> list[dict]:
        """Extract a JSON array from text, trying each [...] match until one parses."""
        # Find all top-level bracket positions and try each as a potential JSON array
        for match in re.finditer(r'\[', text):
            start = match.start()
            depth = 0
            for i in range(start, len(text)):
                if text[i] == '[':
                    depth += 1
                elif text[i] == ']':
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(text[start:i + 1])
                            if isinstance(parsed, list) and parsed:
                                return parsed
                        except (json.JSONDecodeError, ValueError):
                            pass
                        break
        return []

    def _parse_claude_result(self, result: dict) -> list[dict]:
        """Parse Claude's discovery result into a list of topic dicts. Used by both
        normal flow and recovery."""
        raw = result.get("result", "")
        if isinstance(raw, str):
            return self._extract_json_array(raw)
        elif isinstance(raw, dict):
            text = raw.get("result", "") or raw.get("text", "") or str(raw)
            return self._extract_json_array(text)
        elif isinstance(raw, list):
            return raw
        return []

    async def discover_topics(self, run_id: int = None) -> list[dict]:
        """Use Claude Code to discover new blog topics."""
        existing = await self.get_existing_blog_titles()
        logger.info(f"Found {len(existing)} existing blog topics")

        await self._emit("search", f"Searching for {config.DISCOVERY_TOPIC_COUNT} trending topics via Claude Code...")
        await self._emit("search", f"Deduplicating against {len(existing)} existing blog titles")

        result = await self.claude.discover_topics(
            existing_topics=existing,
            focus_areas=FOCUS_AREAS,
            topic_count=config.DISCOVERY_TOPIC_COUNT,
            run_id=run_id,
        )

        if not result.get("success"):
            error_msg = result.get("error", "unknown error")
            logger.error(f"Topic discovery failed: {error_msg}")
            await self._emit("search", f"Topic discovery failed: {error_msg}")
            return []

        await self._emit("search", "Claude session finished, parsing results...")

        topics = self._parse_claude_result(result)
        if not topics:
            await self._emit("search", "Failed to parse topics from Claude response")
            return []

        # Emit each discovered topic
        await self._emit("topics", f"Discovered {len(topics)} topic(s)", {
            "count": len(topics),
            "topics": [
                {
                    "title": t.get("title", "Untitled"),
                    "keywords": t.get("target_keywords", []),
                    "volume": t.get("search_volume_estimate", "?"),
                }
                for t in topics
            ],
        })

        return topics

    async def create_plan_and_issue(self, topic: dict) -> Optional[int]:
        """
        Full SwarmOps planning flow:
        1. POST /api/planning { workspace_id, message } -> session
        2. Poll GET /api/planning/{session.id} until generating==false
        3. POST /api/planning/{session.id}/create-issue -> issue_number

        Returns the issue number if successful, None otherwise.
        """
        title = topic.get("title", "Untitled Blog")
        keywords = topic.get("target_keywords", [])
        outline = topic.get("outline", [])
        spheron_angle = topic.get("spheron_angle", "")

        # Build the planning message
        plan_message = f"""create a blog post plan for this keeping in mind the seo and ai search tips -

**Title:** "{title}"

**Target Keywords:** {', '.join(keywords)}

**Why This Ranks:** {topic.get('why_it_ranks', 'High search volume topic')}

**What to Write:**
{chr(10).join(f'- {s}' for s in outline)}

**Spheron Angle:** {spheron_angle}

**Important Guidelines:**
- Use hyphens (-) instead of emdashes
- Include cross-links to docs.spheron.ai where relevant
- Include cross-links to existing blog posts on spheron.network/blog/
- Fetch current GPU pricing from the Spheron pricing API for accuracy
- Make the content technically accurate and up-to-date
- Follow the existing blog style and formatting conventions in the repo
"""

        try:
            # Step 0: Get workspace ID
            workspace_id = await self._get_workspace_id()
            if not workspace_id:
                logger.error("Could not find SwarmOps workspace for repo")
                await self._emit("planning", f"Could not find SwarmOps workspace for {config.GITHUB_REPO}")
                return None

            # Step 1: Create planning session
            await self._emit("planning", f"Creating SwarmOps planning session for: {title}")
            response = await self.swarmops.create_planning_session(workspace_id, plan_message)
            session = response.get("session", {})
            session_id = session.get("id")

            if not session_id:
                logger.error(f"No session ID in planning response: {response}")
                await self._emit("planning", f"Failed to create planning session for: {title}")
                return None

            logger.info(f"Created planning session: {session_id} for '{title}'")
            await self._emit("planning", f"Planning session created (id: {session_id}), waiting for plan generation...")

            # Step 2: Wait for plan to generate
            try:
                completed = await self.swarmops.wait_for_plan(session_id, timeout=600)
            except TimeoutError:
                logger.error(f"Planning timed out for session {session_id}")
                await self.swarmops.cancel_planning(session_id)
                await self._emit("planning", f"Planning timed out after 600s for: {title}")
                return None

            # Check for errors
            session_status = completed.get("session", {}).get("status", "")
            if session_status == "error":
                plan_content = self.swarmops.extract_plan_from_session(completed)
                logger.error(f"Planning failed for '{title}': {plan_content or 'unknown error'}")
                await self._emit("planning", f"Planning failed for: {title}")
                return None

            # Verify we got a plan
            plan_content = self.swarmops.extract_plan_from_session(completed)
            if not plan_content:
                logger.error(f"No plan generated for session {session_id}")
                await self._emit("planning", f"No plan content generated for: {title}")
                return None

            logger.info(f"Plan generated for '{title}' ({len(plan_content)} chars)")
            await self._emit("planning", f"Plan generated ({len(plan_content)} chars)")

            # Step 3: Create GitHub issue from the plan
            await self._emit("issue", f"Creating GitHub issue from plan...")
            issue_result = await self.swarmops.create_issue_from_plan(
                session_id,
                title=title,
            )

            issue_number = issue_result.get("issue_number")
            issue_url = issue_result.get("issue_url", "")

            if not issue_number:
                logger.error(f"No issue_number in create-issue response: {issue_result}")
                await self._emit("issue", f"Failed to create GitHub issue for: {title}")
                return None

            logger.info(f"Created issue #{issue_number} for '{title}': {issue_url}")
            await self._emit("issue", f"Created issue #{issue_number}: {title}", {
                "issue_number": issue_number,
                "issue_url": issue_url,
            })

            # Add labels
            await self.github.add_labels(issue_number, [config.GITHUB_AGENT_LABEL])

            return issue_number

        except Exception as e:
            logger.error(f"Failed to create plan/issue for '{title}': {e}")
            await self._emit("planning", f"Error: {e}")
            return None

    async def trigger_writing(self, issue_number: int):
        """
        Comment on the issue to trigger SwarmOps writing agent.
        SwarmOps watches for TRIGGER_MENTION in issue comments.
        """
        await self._emit("trigger", f"Triggering writing agent on issue #{issue_number}...")
        await self.github.add_issue_comment(
            issue_number,
            config.SWARMOPS_TRIGGER_MENTION,
        )
        logger.info(f"Triggered writing agent for issue #{issue_number}")
        await self._emit("trigger", f"Writing agent triggered for issue #{issue_number}")

    async def process_topics(self, topics: list[dict], run_id: int, recovered: bool = False):
        """
        Process discovered topics: save to DB, create plans, issues, and trigger writing.
        This method is idempotent - safe to call after a server restart. It checks each
        topic's status in the DB and resumes from where it left off.
        """
        if recovered:
            await self._emit("start", "Resuming discovery pipeline after server restart")

        created_count = 0
        total = len(topics)

        for i, topic in enumerate(topics, 1):
            title = topic.get("title", "")
            keywords = topic.get("target_keywords", [])
            if not title:
                continue

            await self._emit("topics", f"[{i}/{total}] Processing: {title}", {
                "title": title,
                "keywords": keywords,
                "index": i,
                "total": total,
            })

            # Check if topic already exists in DB
            existing_topic = await self.db.get_topic_by_title(title)

            if existing_topic:
                status = existing_topic.get("status", "")
                topic_id = existing_topic["id"]
                issue_number = existing_topic.get("issue_number")

                # Already fully processed
                if status in ("writing", "pr_created", "reviewing", "ready", "completed"):
                    await self._emit("topics", f"[{i}/{total}] Already in progress (status: {status}): {title}")
                    continue

                # Failed previously - skip
                if status == "planning_failed":
                    await self._emit("topics", f"[{i}/{total}] Previously failed, skipping: {title}")
                    continue

                # Stuck in planning - retry
                if status == "planning":
                    await self._emit("topics", f"[{i}/{total}] Resuming planning for: {title}")
                    issue_number = await self.create_plan_and_issue(topic)
                    if issue_number:
                        await self.db.update_topic_status(topic_id, "issue_created", issue_number=issue_number)
                        await self.trigger_writing(issue_number)
                        await self.db.update_topic_status(topic_id, "writing")
                        created_count += 1
                        await self._emit("topics", f"[{i}/{total}] Complete - issue #{issue_number} is now being written")
                    else:
                        await self.db.update_topic_status(
                            topic_id, "planning_failed",
                            metadata=json.dumps({"error": "Plan or issue creation failed"}),
                        )
                        await self._emit("topics", f"[{i}/{total}] Failed - planning or issue creation failed for: {title}")
                    continue

                # Has issue but writing not triggered yet
                if status == "issue_created" and issue_number:
                    await self._emit("topics", f"[{i}/{total}] Resuming - triggering writing for issue #{issue_number}")
                    await self.trigger_writing(issue_number)
                    await self.db.update_topic_status(topic_id, "writing")
                    created_count += 1
                    await self._emit("topics", f"[{i}/{total}] Complete - issue #{issue_number} is now being written")
                    continue

                # Topic exists but hasn't started planning (status: discovered)
                if status == "discovered":
                    await self._emit("topics", f"[{i}/{total}] Resuming from discovered state: {title}")
                    # Fall through to planning below
                else:
                    # Duplicate from a previous run with unknown state - skip
                    await self._emit("topics", f"[{i}/{total}] Skipped (already exists, status: {status}): {title}")
                    continue
            else:
                # New topic - save to DB
                topic_id = await self.db.create_topic(
                    title=title,
                    keywords=topic.get("target_keywords", []),
                    outline=topic.get("outline", []),
                    spheron_angle=topic.get("spheron_angle", ""),
                    search_volume=topic.get("search_volume_estimate", "medium"),
                )
                await self._emit("topics", f"[{i}/{total}] Saved to database (topic #{topic_id})")

            # Create plan and issue via SwarmOps
            await self.db.update_topic_status(topic_id, "planning")

            # Save progress to recovery context so restart can continue from here
            await self.db.update_agent_run_recovery_context(run_id, {
                "type": "discovery",
                "topics": topics,
                "current_index": i,
            })

            issue_number = await self.create_plan_and_issue(topic)

            if issue_number:
                await self.db.update_topic_status(
                    topic_id, "issue_created",
                    issue_number=issue_number,
                )

                # Trigger writing agent
                await self.trigger_writing(issue_number)
                await self.db.update_topic_status(topic_id, "writing")
                created_count += 1

                await self._emit("topics", f"[{i}/{total}] Complete - issue #{issue_number} is now being written")

                # Small delay between issues to avoid rate limiting
                if i < total:
                    await asyncio.sleep(5)
            else:
                await self.db.update_topic_status(
                    topic_id, "planning_failed",
                    metadata=json.dumps({"error": "Plan or issue creation failed"}),
                )
                await self._emit("topics", f"[{i}/{total}] Failed - planning or issue creation failed for: {title}")

        return created_count, total

    async def run(self):
        """
        Full discovery pipeline:
        1. Discover topics via Claude Code + web search
        2. Save to DB
        3. Create plans and issues via SwarmOps API
        4. Trigger writing agent via GitHub comment
        """
        run_id = await self.db.create_agent_run("discovery")
        self._run_id = run_id
        logger.info("Starting blog discovery run...")

        # Save recovery context so restart knows this is a discovery run
        await self.db.update_agent_run_recovery_context(run_id, {"type": "discovery"})

        try:
            await self._emit("start", "Discovery pipeline started")

            topics = await self.discover_topics(run_id=run_id)
            if not topics:
                logger.warning("No topics discovered")
                await self._emit("done", "No new topics found")
                await self.db.finish_agent_run(run_id, "completed", {"topics_found": 0})
                return

            logger.info(f"Discovered {len(topics)} topics")

            # Save topics to recovery context so restart can continue processing
            await self.db.update_agent_run_recovery_context(run_id, {
                "type": "discovery",
                "topics": topics,
            })

            created_count, total = await self.process_topics(topics, run_id)

            await self._emit("done", f"Discovery complete: {created_count}/{total} topics sent to writing", {
                "topics_found": total,
                "issues_created": created_count,
            })
            await self.db.finish_agent_run(
                run_id, "completed",
                {"topics_found": total, "issues_created": created_count},
            )
            logger.info(f"Discovery complete: {created_count}/{total} topics processed")

        except Exception as e:
            logger.error(f"Discovery run failed: {e}")
            await self._emit("error", f"Discovery failed: {e}")
            await self.db.finish_agent_run(run_id, "error", error=str(e))
        finally:
            self._run_id = None
