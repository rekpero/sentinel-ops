"""
Blog Review Agent - monitors PRs labeled 'blog', performs editorial review,
fact-checking, pricing verification, and iterates until the blog is ready.

Key behaviors:
- Posts resolvable LINE comments for SwarmOps to act on
- Posts general PR comments for tagging humans (non-resolvable)
- Monitors commits and re-reviews until satisfied
- Resolves comments when issues are fixed
- Tags @rekpero when the blog is ready to merge
"""
import asyncio
import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from backend.services.github_service import GitHubService
from backend.services.claude_service import ClaudeService
from backend.services.pricing_service import PricingService
from backend.db.database import Database
from backend import config

logger = logging.getLogger(__name__)

# Minimum score to consider a blog ready
MIN_ACCEPTABLE_SCORE = 7.5


class ReviewAgent:
    def __init__(
        self,
        github: GitHubService,
        claude: ClaudeService,
        pricing: PricingService,
        db: Database,
    ):
        self.github = github
        self.claude = claude
        self.pricing = pricing
        self.db = db
        self._tracked_prs: dict[int, str] = {}  # pr_number -> last_seen_commit_sha

    async def ensure_repo_cloned(self):
        """Clone or update the target repository for full context."""
        repo_dir = config.REPO_CLONE_DIR
        repo_dir.parent.mkdir(parents=True, exist_ok=True)

        if repo_dir.exists() and (repo_dir / ".git").exists():
            # Pull latest
            logger.info("Updating repository clone...")
            result = subprocess.run(
                ["git", "pull", "--rebase"],
                cwd=str(repo_dir),
                capture_output=True, text=True, timeout=120,
                env={**subprocess.os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            if result.returncode != 0:
                logger.warning(f"Git pull failed: {result.stderr}")
        else:
            # Clone fresh
            logger.info(f"Cloning repository {config.GITHUB_REPO}...")
            clone_url = f"https://x-access-token:{config.GITHUB_TOKEN}@github.com/{config.GITHUB_REPO}.git"
            result = subprocess.run(
                ["git", "clone", clone_url, str(repo_dir)],
                capture_output=True, text=True, timeout=300,
                env={**subprocess.os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            if result.returncode != 0:
                logger.error(f"Git clone failed: {result.stderr}")
                return False

        return True

    async def get_existing_blogs_context(self) -> str:
        """Read existing blog titles and slugs for cross-linking context."""
        repo_dir = config.REPO_CLONE_DIR
        context_lines = []

        try:
            result = subprocess.run(
                ["find", str(repo_dir), "-path", "*/blog*", "-name", "*.md*",
                 "-not", "-path", "*/node_modules/*"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for fpath in result.stdout.strip().split("\n"):
                    if fpath:
                        p = Path(fpath)
                        slug = p.stem
                        context_lines.append(f"- {slug}: {config.SPHERON_BLOG_URL}{slug}/")
        except Exception as e:
            logger.warning(f"Could not scan for existing blogs: {e}")

        return "\n".join(context_lines[:50])  # Limit context size

    async def get_pr_blog_content(self, pr_number: int) -> tuple[str, list[dict]]:
        """
        Get the blog content from a PR's changed files.
        Returns (content, files) where files is the list of changed file info.
        """
        files = await self.github.get_pr_files(pr_number)
        blog_content = ""
        blog_files = []

        for f in files:
            filename = f.get("filename", "")
            # Look for blog content files (MD, MDX, etc.)
            if any(ext in filename.lower() for ext in [".md", ".mdx"]) and "blog" in filename.lower():
                blog_files.append(f)
                # Try to read the actual file content from the repo
                patch = f.get("patch", "")
                if patch:
                    # Extract added lines from the patch
                    added_lines = []
                    for line in patch.split("\n"):
                        if line.startswith("+") and not line.startswith("+++"):
                            added_lines.append(line[1:])
                    blog_content += "\n".join(added_lines) + "\n"

        # If we have the repo cloned, try to read the full file
        if blog_files and config.REPO_CLONE_DIR.exists():
            pr_data = await self.github.get_pr(pr_number)
            branch = pr_data.get("head", {}).get("ref", "")
            if branch:
                try:
                    subprocess.run(
                        ["git", "fetch", "origin", branch],
                        cwd=str(config.REPO_CLONE_DIR),
                        capture_output=True, timeout=30,
                    )
                    for bf in blog_files:
                        fpath = config.REPO_CLONE_DIR / bf["filename"]
                        result = subprocess.run(
                            ["git", "show", f"origin/{branch}:{bf['filename']}"],
                            cwd=str(config.REPO_CLONE_DIR),
                            capture_output=True, text=True, timeout=10,
                        )
                        if result.returncode == 0:
                            blog_content = result.stdout
                            break
                except Exception as e:
                    logger.warning(f"Could not read full file from branch: {e}")

        return blog_content, blog_files

    def _strip_emdashes(self, text: str) -> str:
        """Remove all emdashes from text, replace with hyphens."""
        return text.replace("\u2014", "-").replace("\u2013", "-").replace("--", "-")

    async def perform_editorial_review(self, pr_number: int, blog_content: str) -> dict:
        """Run Claude Code editorial review on the blog."""
        existing_context = await self.get_existing_blogs_context()

        result = await self.claude.review_blog(
            blog_content=blog_content,
            repo_path=str(config.REPO_CLONE_DIR),
            existing_blogs_context=existing_context,
        )

        if not result.get("success"):
            logger.error(f"Editorial review failed: {result.get('error')}")
            return {}

        # Parse the review result
        raw = result.get("result", "")
        if isinstance(raw, dict):
            raw = raw.get("result", "") or raw.get("text", "") or json.dumps(raw)

        try:
            json_match = re.search(r'\{[\s\S]*\}', str(raw))
            if json_match:
                review = json.loads(json_match.group())
                return review
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"Failed to parse review result: {e}")

        return {}

    async def perform_fact_check(self, pr_number: int, blog_content: str) -> dict:
        """Run Claude Code fact-check on the blog."""
        result = await self.claude.fact_check_blog(
            blog_content=blog_content,
            repo_path=str(config.REPO_CLONE_DIR),
        )

        if not result.get("success"):
            logger.error(f"Fact check failed: {result.get('error')}")
            return {}

        raw = result.get("result", "")
        if isinstance(raw, dict):
            raw = raw.get("result", "") or raw.get("text", "") or json.dumps(raw)

        try:
            json_match = re.search(r'\{[\s\S]*\}', str(raw))
            if json_match:
                return json.loads(json_match.group())
        except (json.JSONDecodeError, TypeError):
            pass

        return {}

    async def check_pricing(self, blog_content: str) -> tuple[list[dict], str]:
        """Check GPU pricing in the blog against current API prices."""
        mismatches = await self.pricing.check_blog_pricing(blog_content)
        pricing_table = await self.pricing.format_pricing_for_comment()
        return mismatches, pricing_table

    async def post_review_comments(
        self,
        pr_number: int,
        review: dict,
        fact_check: dict,
        pricing_mismatches: list[dict],
        pricing_table: str,
        blog_files: list[dict],
    ) -> list[int]:
        """
        Post line comments on the PR for SwarmOps to act on.
        These are resolvable comments tied to specific lines.
        """
        commit_sha = await self.github.get_latest_commit_sha(pr_number)
        if not commit_sha or not blog_files:
            logger.error("No commit SHA or blog files found for review comments")
            return []

        # Use the first blog file for line comments
        blog_file = blog_files[0]
        filepath = blog_file.get("filename", "")
        comment_ids = []

        # === Build the main review comment ===
        score = review.get("overall_score", 0)
        scores = review.get("scores", {})
        improvements = review.get("improvements", [])
        fact_flags = review.get("fact_check_flags", []) + fact_check.get("outdated_items", [])
        missing_links = review.get("missing_crosslinks", [])
        stale_data = fact_check.get("stale_data", [])

        # Build editorial review body
        review_body = self._strip_emdashes(f"""## Blog Review - Editorial & Content Quality

**Overall Score: {score}/10**

| Category | Score |
|----------|-------|
| Content Quality | {scores.get('content_quality', 'N/A')}/10 |
| SEO Optimization | {scores.get('seo_optimization', 'N/A')}/10 |
| Technical Accuracy | {scores.get('technical_accuracy', 'N/A')}/10 |
| Readability | {scores.get('readability', 'N/A')}/10 |
| Internal Linking | {scores.get('internal_linking', 'N/A')}/10 |

**Summary:** {review.get('summary', 'Review complete.')}

### Improvements Needed:
""")

        for imp in improvements:
            severity_icon = {"high": "!!!", "medium": "!!", "low": "!"}.get(imp.get("severity", "low"), "!")
            review_body += self._strip_emdashes(
                f"\n- [{severity_icon}] **{imp.get('type', 'general').upper()}**: "
                f"{imp.get('description', '')} - *Suggestion: {imp.get('suggestion', '')}*"
            )

        if missing_links:
            review_body += "\n\n### Missing Cross-links:\n"
            for link in missing_links:
                review_body += self._strip_emdashes(
                    f"\n- In \"{link.get('context', '')}\": "
                    f"Add link to [{link.get('anchor_text', 'docs')}]({link.get('suggested_link', '')})"
                )

        # Post as line comment (resolvable) on line 1 of the blog file
        try:
            comment = await self.github.create_pr_review_comment(
                pr_number=pr_number,
                body=review_body,
                commit_id=commit_sha,
                path=filepath,
                line=1,
            )
            comment_ids.append(comment.get("id"))
        except Exception as e:
            logger.error(f"Failed to post review comment: {e}")

        # === Fact-check and pricing comment ===
        fact_body = self._strip_emdashes("## Fact-Check & Pricing Update\n\n")

        if fact_flags:
            fact_body += "### Items to Fact-Check:\n"
            for item in fact_flags:
                claim = item.get("claim", item.get("data_point", ""))
                concern = item.get("concern", item.get("current_info", ""))
                suggestion = item.get("suggestion", item.get("correction", ""))
                fact_body += self._strip_emdashes(
                    f"\n- **Claim:** \"{claim}\"\n  **Concern:** {concern}\n  **Fix:** {suggestion}\n"
                )

        if stale_data:
            fact_body += "\n### Stale Data to Update:\n"
            for item in stale_data:
                fact_body += self._strip_emdashes(
                    f"\n- **{item.get('data_point', '')}** - Update to: {item.get('correction', '')} "
                    f"(Source: {item.get('source', 'N/A')})\n"
                )

        if pricing_mismatches:
            fact_body += "\n### GPU Pricing Mismatches:\n"
            for m in pricing_mismatches:
                spot_info = f", spot is ${m['current_spot']}/hr" if m.get('current_spot') else ""
                fact_body += self._strip_emdashes(
                    f"\n- **{m['display_name']}**: Blog says ${m['mentioned_price']}/hr, "
                    f"but current on-demand is ${m.get('current_ondemand', 'N/A')}/hr"
                    f"{spot_info}\n"
                )

        fact_body += f"\n\n{pricing_table}\n"
        fact_body += self._strip_emdashes(
            "\n*Note: Pricing is based on current date and can fluctuate over time "
            "based on availability of the GPUs.*"
        )

        fact_body += self._strip_emdashes(
            "\n\n**Action Required:** Please do full fact check on this blog with websearch "
            "and webfetch so that there is no outdated or stale data or content mentioned in this blog. "
            "Do this till you can't find any outdated data and then finally resolve this comment. "
            "Make sure to remove all the emdashes or whenever you update data, don't use any emdash in the content. "
            "Also check [current GPU pricing](/pricing/) for live rates."
        )

        # Post fact-check as another line comment (pick a different line if possible)
        try:
            # Use line 5 or whatever is available
            target_line = min(5, blog_file.get("changes", 10))
            if target_line < 1:
                target_line = 1
            comment = await self.github.create_pr_review_comment(
                pr_number=pr_number,
                body=fact_body,
                commit_id=commit_sha,
                path=filepath,
                line=target_line,
            )
            comment_ids.append(comment.get("id"))
        except Exception as e:
            logger.error(f"Failed to post fact-check comment: {e}")
            # Fallback: try line 1
            try:
                comment = await self.github.create_pr_review_comment(
                    pr_number=pr_number,
                    body=fact_body,
                    commit_id=commit_sha,
                    path=filepath,
                    line=1,
                )
                comment_ids.append(comment.get("id"))
            except Exception as e2:
                logger.error(f"Fallback comment also failed: {e2}")

        return comment_ids

    async def check_if_blog_ready(self, pr_number: int) -> bool:
        """
        Check if the blog is ready by verifying:
        1. All review comments have been addressed (check for new commits since last review)
        2. The latest review score is above threshold
        3. Fact-check and pricing are all resolved
        """
        # Check unresolved threads
        unresolved = await self.github.get_unresolved_review_threads(pr_number)
        if unresolved:
            logger.info(f"PR #{pr_number} still has {len(unresolved)} unresolved threads")
            return False

        # Check latest review score
        reviews = await self.db.get_review_logs(pr_number)
        if reviews:
            latest = reviews[-1]
            if latest.get("score", 0) >= MIN_ACCEPTABLE_SCORE:
                return True

        return False

    async def notify_ready(self, pr_number: int, topic_title: str):
        """Tag the reviewer that the blog is ready to merge."""
        body = self._strip_emdashes(
            f"@{config.GITHUB_REVIEWER_USERNAME} The blog \"{topic_title}\" has passed all "
            f"editorial reviews, fact-checks, and pricing verifications. "
            f"It is ready for your final review and merge. "
        )
        await self.github.add_pr_comment(pr_number, body)
        logger.info(f"Notified @{config.GITHUB_REVIEWER_USERNAME} that PR #{pr_number} is ready")

    async def review_pr(self, pr_number: int) -> bool:
        """
        Full review cycle for a single PR.
        Returns True if the blog is ready, False if it needs more work.
        """
        logger.info(f"Starting review of PR #{pr_number}")

        # Get the blog content
        blog_content, blog_files = await self.get_pr_blog_content(pr_number)
        if not blog_content:
            logger.warning(f"No blog content found in PR #{pr_number}")
            return False

        # Get or create topic record
        topic = await self.db.get_topic_by_pr(pr_number)
        topic_id = topic["id"] if topic else None

        if not topic_id:
            # Try to find by issue reference in PR body
            pr_data = await self.github.get_pr(pr_number)
            pr_body = pr_data.get("body", "")
            issue_match = re.search(r'#(\d+)', pr_body)
            if issue_match:
                issue_num = int(issue_match.group(1))
                topic = await self.db.get_topic_by_issue(issue_num)
                if topic:
                    topic_id = topic["id"]
                    await self.db.update_topic_status(topic_id, "pr_created", pr_number=pr_number)

        if not topic_id:
            # Create a new topic entry for this PR
            pr_data = await self.github.get_pr(pr_number)
            topic_id = await self.db.create_topic(
                title=pr_data.get("title", f"PR #{pr_number}"),
            )
            await self.db.update_topic_status(topic_id, "reviewing", pr_number=pr_number)

        # Get current iteration
        iteration = await self.db.get_latest_review_iteration(pr_number) + 1

        if iteration > config.MAX_REVIEW_ITERATIONS:
            logger.warning(f"PR #{pr_number} exceeded max review iterations ({config.MAX_REVIEW_ITERATIONS})")
            await self.github.add_pr_comment(
                pr_number,
                self._strip_emdashes(
                    f"@{config.GITHUB_REVIEWER_USERNAME} This blog has gone through "
                    f"{config.MAX_REVIEW_ITERATIONS} review iterations. Please review manually."
                ),
            )
            await self.db.update_topic_status(topic_id, "needs_human")
            return False

        # Update status
        await self.db.update_topic_status(topic_id, "reviewing")

        # Run editorial review and fact-check in parallel
        editorial_task = self.perform_editorial_review(pr_number, blog_content)
        fact_task = self.perform_fact_check(pr_number, blog_content)
        pricing_task = self.check_pricing(blog_content)

        review, fact_check, (pricing_mismatches, pricing_table) = await asyncio.gather(
            editorial_task, fact_task, pricing_task
        )

        # Calculate overall score
        score = review.get("overall_score", 0)

        # Log the review
        await self.db.create_review_log(
            topic_id=topic_id,
            pr_number=pr_number,
            iteration=iteration,
            review_type="full",
            score=score,
            review_data={
                "editorial": review,
                "fact_check": fact_check,
                "pricing_mismatches": pricing_mismatches,
            },
        )
        await self.db.update_topic_status(topic_id, "reviewing",
                                          review_score=score,
                                          review_iterations=iteration)

        # Check if blog needs work
        has_issues = (
            len(review.get("improvements", [])) > 0
            or len(fact_check.get("outdated_items", [])) > 0
            or len(fact_check.get("stale_data", [])) > 0
            or len(pricing_mismatches) > 0
        )

        if has_issues or score < MIN_ACCEPTABLE_SCORE:
            # Post review comments for SwarmOps to act on
            comment_ids = await self.post_review_comments(
                pr_number=pr_number,
                review=review,
                fact_check=fact_check,
                pricing_mismatches=pricing_mismatches,
                pricing_table=pricing_table,
                blog_files=blog_files,
            )
            logger.info(
                f"PR #{pr_number} review iteration {iteration}: "
                f"Score {score}/10, posted {len(comment_ids)} comments"
            )
            return False
        else:
            # Blog is good!
            topic = await self.db.get_topic(topic_id)
            title = topic.get("title", f"PR #{pr_number}") if topic else f"PR #{pr_number}"
            await self.notify_ready(pr_number, title)
            await self.db.update_topic_status(topic_id, "ready")
            return True

    async def poll_prs(self):
        """
        Poll for PRs labeled 'blog' and review new/updated ones.
        This is the main loop for the review agent.
        """
        logger.info("Polling for blog PRs...")

        # Ensure repo is cloned and up to date
        await self.ensure_repo_cloned()

        # Get open PRs with blog label
        prs = await self.github.list_prs(state="open", labels=config.GITHUB_BLOG_LABEL)
        logger.info(f"Found {len(prs)} open PRs with '{config.GITHUB_BLOG_LABEL}' label")

        if not prs:
            logger.info("No open blog PRs found - nothing to review")
            return

        for pr_item in prs:
            pr_number = pr_item.get("number")
            pr_title = pr_item.get("title", "untitled")
            if not pr_number:
                continue

            # Get full PR data
            try:
                pr_data = await self.github.get_pr(pr_number)
            except Exception as e:
                logger.error(f"Failed to get PR #{pr_number}: {e}")
                continue

            latest_sha = pr_data.get("head", {}).get("sha", "")
            last_seen = self._tracked_prs.get(pr_number, "")

            if latest_sha == last_seen:
                logger.info(
                    f"PR #{pr_number} ({pr_title}): no new commits (sha: {latest_sha[:7]}), "
                    f"checking if ready..."
                )
                # No new commits since last review, check if all comments are resolved
                is_ready = await self.check_if_blog_ready(pr_number)
                if is_ready:
                    topic = await self.db.get_topic_by_pr(pr_number)
                    if topic and topic.get("status") != "ready":
                        title = topic.get("title", f"PR #{pr_number}")
                        await self.notify_ready(pr_number, title)
                        await self.db.update_topic_status(topic["id"], "ready")
                        logger.info(f"PR #{pr_number} ({pr_title}): marked as ready!")
                    else:
                        logger.info(f"PR #{pr_number} ({pr_title}): already marked ready or no topic")
                else:
                    logger.info(f"PR #{pr_number} ({pr_title}): not ready yet, waiting for fixes")
                continue

            # New commits detected - run review
            if last_seen:
                logger.info(
                    f"PR #{pr_number} ({pr_title}): new commits detected "
                    f"({last_seen[:7]} -> {latest_sha[:7]}), starting review..."
                )
            else:
                logger.info(
                    f"PR #{pr_number} ({pr_title}): first time seeing this PR "
                    f"(sha: {latest_sha[:7]}), starting review..."
                )

            # Update the repo clone to get the latest changes
            await self.ensure_repo_cloned()

            # Run the review
            run_id = await self.db.create_agent_run("reviewer", None)
            try:
                is_ready = await self.review_pr(pr_number)
                status = "ready" if is_ready else "needs work"
                logger.info(f"PR #{pr_number} ({pr_title}): review complete - {status}")
                await self.db.finish_agent_run(
                    run_id, "completed",
                    {"pr_number": pr_number, "ready": is_ready},
                )
            except Exception as e:
                logger.error(f"Review failed for PR #{pr_number} ({pr_title}): {e}", exc_info=True)
                await self.db.finish_agent_run(run_id, "error", error=str(e))

            # Track this commit
            self._tracked_prs[pr_number] = latest_sha

    async def run_continuous(self):
        """Run the review agent continuously, polling at configured intervals."""
        logger.info(f"Starting review agent (poll interval: {config.PR_POLL_INTERVAL_SECONDS}s)")
        while True:
            try:
                await self.poll_prs()
            except Exception as e:
                logger.error(f"Review poll error: {e}")
            await asyncio.sleep(config.PR_POLL_INTERVAL_SECONDS)
