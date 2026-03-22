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

    async def discover_topics(self) -> list[dict]:
        """Use Claude Code to discover new blog topics."""
        existing = await self.get_existing_blog_titles()
        logger.info(f"Found {len(existing)} existing blog topics")

        result = await self.claude.discover_topics(
            existing_topics=existing,
            focus_areas=FOCUS_AREAS,
            topic_count=config.DISCOVERY_TOPIC_COUNT,
        )

        if not result.get("success"):
            logger.error(f"Topic discovery failed: {result.get('error')}")
            return []

        # Parse the result
        raw = result.get("result", "")
        if isinstance(raw, str):
            try:
                import re
                json_match = re.search(r'\[[\s\S]*\]', raw)
                if json_match:
                    topics = json.loads(json_match.group())
                else:
                    logger.error("Could not find JSON array in discovery result")
                    return []
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse discovery result: {e}")
                return []
        elif isinstance(raw, dict):
            text = raw.get("result", "") or raw.get("text", "") or str(raw)
            try:
                import re
                json_match = re.search(r'\[[\s\S]*\]', text)
                if json_match:
                    topics = json.loads(json_match.group())
                else:
                    return []
            except (json.JSONDecodeError, TypeError):
                return []
        elif isinstance(raw, list):
            topics = raw
        else:
            return []

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
                return None

            # Step 1: Create planning session
            # POST /api/planning -> { "session": {...}, "messages": [...] }
            response = await self.swarmops.create_planning_session(workspace_id, plan_message)
            session = response.get("session", {})
            session_id = session.get("id")

            if not session_id:
                logger.error(f"No session ID in planning response: {response}")
                return None

            logger.info(f"Created planning session: {session_id} for '{title}'")

            # Step 2: Wait for plan to generate
            # Poll until generating==false (plan is the last assistant message)
            try:
                completed = await self.swarmops.wait_for_plan(session_id, timeout=600)
            except TimeoutError:
                logger.error(f"Planning timed out for session {session_id}")
                await self.swarmops.cancel_planning(session_id)
                return None

            # Check for errors
            session_status = completed.get("session", {}).get("status", "")
            if session_status == "error":
                plan_content = self.swarmops.extract_plan_from_session(completed)
                logger.error(f"Planning failed for '{title}': {plan_content or 'unknown error'}")
                return None

            # Verify we got a plan
            plan_content = self.swarmops.extract_plan_from_session(completed)
            if not plan_content:
                logger.error(f"No plan generated for session {session_id}")
                return None

            logger.info(f"Plan generated for '{title}' ({len(plan_content)} chars)")

            # Step 3: Create GitHub issue from the plan
            # POST /api/planning/{id}/create-issue -> { issue_number, issue_url, title }
            issue_result = await self.swarmops.create_issue_from_plan(
                session_id,
                title=title,  # Pass our SEO title
            )

            issue_number = issue_result.get("issue_number")
            issue_url = issue_result.get("issue_url", "")

            if not issue_number:
                logger.error(f"No issue_number in create-issue response: {issue_result}")
                return None

            logger.info(f"Created issue #{issue_number} for '{title}': {issue_url}")

            # The issue already gets the ISSUE_LABEL from SwarmOps (default: "agent")
            # Add blog label too for our tracking
            await self.github.add_labels(issue_number, [config.GITHUB_AGENT_LABEL])

            return issue_number

        except Exception as e:
            logger.error(f"Failed to create plan/issue for '{title}': {e}")
            return None

    async def trigger_writing(self, issue_number: int):
        """
        Comment on the issue to trigger SwarmOps writing agent.
        SwarmOps watches for TRIGGER_MENTION in issue comments.
        """
        await self.github.add_issue_comment(
            issue_number,
            config.SWARMOPS_TRIGGER_MENTION,
        )
        logger.info(f"Triggered writing agent for issue #{issue_number}")

    async def run(self):
        """
        Full discovery pipeline:
        1. Discover topics via Claude Code + web search
        2. Save to DB
        3. Create plans and issues via SwarmOps API
        4. Trigger writing agent via GitHub comment
        """
        run_id = await self.db.create_agent_run("discovery")
        logger.info("Starting blog discovery run...")

        try:
            topics = await self.discover_topics()
            if not topics:
                logger.warning("No topics discovered")
                await self.db.finish_agent_run(run_id, "completed", {"topics_found": 0})
                return

            logger.info(f"Discovered {len(topics)} topics")
            created_count = 0

            for topic in topics:
                title = topic.get("title", "")
                if not title:
                    continue

                # Check if topic already exists
                if await self.db.topic_title_exists(title):
                    logger.info(f"Topic already exists, skipping: {title}")
                    continue

                # Save to DB
                topic_id = await self.db.create_topic(
                    title=title,
                    keywords=topic.get("target_keywords", []),
                    outline=topic.get("outline", []),
                    spheron_angle=topic.get("spheron_angle", ""),
                    search_volume=topic.get("search_volume_estimate", "medium"),
                )

                # Create plan and issue via SwarmOps
                await self.db.update_topic_status(topic_id, "planning")
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

                    # Small delay between issues to avoid rate limiting
                    await asyncio.sleep(5)
                else:
                    await self.db.update_topic_status(
                        topic_id, "planning_failed",
                        metadata=json.dumps({"error": "Plan or issue creation failed"}),
                    )

            await self.db.finish_agent_run(
                run_id, "completed",
                {"topics_found": len(topics), "issues_created": created_count},
            )
            logger.info(f"Discovery complete: {created_count}/{len(topics)} topics processed")

        except Exception as e:
            logger.error(f"Discovery run failed: {e}")
            await self.db.finish_agent_run(run_id, "error", error=str(e))
