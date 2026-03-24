"""
Blog Review Agent - monitors PRs labeled 'blog', performs editorial review,
fact-checking, pricing verification, and iterates until the blog is ready.

Key behaviors:
- Single Claude Code session does fact-check + editorial + posts resolvable inline comment
- Monitors commits and re-reviews until satisfied
- Tags reviewer when the blog is ready to merge
"""
import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path

from backend.services.github_service import GitHubService
from backend.services.claude_service import ClaudeService
from backend.services.pricing_service import PricingService
from backend.db.database import Database
from backend import config

logger = logging.getLogger(__name__)



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

    # === Worktree management ===

    def _pr_worktree_path(self, pr_number: int) -> Path:
        return config.WORKDIR / "pr-reviews" / f"pr-{pr_number}"

    async def setup_pr_worktree(self, pr_number: int) -> tuple[str, str]:
        """
        Fetch the PR branch and create a git worktree so blog files
        actually exist on disk for Claude to read.
        Returns (worktree_path, branch_name). Path is empty string on failure.
        """
        pr_data = await self.github.get_pr(pr_number)
        branch = pr_data.get("head", {}).get("ref", "")
        if not branch:
            logger.error(f"Could not determine branch for PR #{pr_number}")
            return "", ""

        worktree_path = self._pr_worktree_path(pr_number)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove any stale worktree first
        if worktree_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", str(worktree_path), "--force"],
                cwd=str(config.REPO_CLONE_DIR), capture_output=True, timeout=30,
            )

        # Fetch the branch
        fetch = subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=str(config.REPO_CLONE_DIR), capture_output=True, text=True, timeout=60,
            env={**subprocess.os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if fetch.returncode != 0:
            logger.error(f"git fetch failed: {fetch.stderr}")
            return "", branch

        # Create worktree checked out to the PR branch
        result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_path), f"origin/{branch}"],
            cwd=str(config.REPO_CLONE_DIR), capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.error(f"git worktree add failed: {result.stderr}")
            return "", branch

        logger.info(f"PR #{pr_number}: worktree created at {worktree_path} (branch: {branch})")
        return str(worktree_path), branch

    def cleanup_pr_worktree(self, pr_number: int):
        """Remove the git worktree created for this PR review."""
        worktree_path = self._pr_worktree_path(pr_number)
        if worktree_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", str(worktree_path), "--force"],
                cwd=str(config.REPO_CLONE_DIR), capture_output=True, timeout=30,
            )
            logger.info(f"PR #{pr_number}: cleaned up worktree at {worktree_path}")

    # === Repository helpers ===

    async def ensure_repo_cloned(self):
        """Clone or update the target repository for full context."""
        repo_dir = config.REPO_CLONE_DIR
        repo_dir.parent.mkdir(parents=True, exist_ok=True)

        if repo_dir.exists() and (repo_dir / ".git").exists():
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

        return "\n".join(context_lines[:50])

    # === Content extraction ===

    async def get_pr_blog_content(self, pr_number: int, worktree_path: str = "") -> tuple[str, list[dict], str]:
        """
        Get the primary blog content from a PR's changed files.
        Identifies the PRIMARY blog (most additions = new blog, not backlink edits).
        Reads from worktree_path where the PR branch is checked out.
        Returns (content, all_blog_files, abs_primary_file_path).
        """
        files = await self.github.get_pr_files(pr_number)
        blog_files = [
            f for f in files
            if any(ext in f.get("filename", "").lower() for ext in [".md", ".mdx"])
            and "blog" in f.get("filename", "").lower()
        ]

        if not blog_files:
            return "", [], ""

        # Primary blog = most additions (new blog >> backlink edits)
        primary_file = max(blog_files, key=lambda f: f.get("additions", 0))
        primary_path = primary_file.get("filename", "")
        logger.info(
            f"PR #{pr_number}: primary blog = {primary_path} ({primary_file.get('additions', 0)} additions)"
        )
        for bf in blog_files:
            if bf["filename"] != primary_path:
                logger.info(f"  Skipping backlink edit: {bf['filename']} ({bf.get('additions', 0)} additions)")

        blog_content = ""
        base = Path(worktree_path) if worktree_path else None

        # Read directly from worktree (branch is checked out there)
        if base and base.exists():
            fpath = base / primary_path
            if fpath.exists():
                blog_content = fpath.read_text(encoding="utf-8", errors="replace")
                logger.info(f"Read {len(blog_content)} chars from worktree: {fpath}")
            else:
                logger.warning(f"File not found in worktree: {fpath}")

        # Fallback: extract added lines from the patch
        if not blog_content:
            patch = primary_file.get("patch", "")
            if patch:
                added_lines = [
                    line[1:] for line in patch.split("\n")
                    if line.startswith("+") and not line.startswith("+++")
                ]
                blog_content = "\n".join(added_lines)
                logger.info(f"Fallback: extracted {len(added_lines)} added lines from patch")

        abs_primary_path = str((base / primary_path) if base else Path(primary_path))
        return blog_content, blog_files, abs_primary_path

    # === Ready check ===

    def _strip_emdashes(self, text: str) -> str:
        """Remove all emdashes from text, replace with hyphens."""
        return text.replace("\u2014", "-").replace("\u2013", "-").replace("--", "-")

    async def check_if_blog_ready(self, pr_number: int) -> bool:
        """
        Check if the blog is ready:
        1. No unresolved sentinel review threads (threads posted by our bot)
        2. No pending CHANGES_REQUESTED reviews
        3. Latest review score is above threshold
        """
        sentinel_marker = f"sentinel-review:pr-{pr_number}"
        unresolved = await self.github.get_unresolved_review_threads(pr_number)
        # Only count threads that contain our sentinel marker - ignore human reviewer threads
        sentinel_unresolved = [
            t for t in unresolved
            if any(
                sentinel_marker in c.get("body", "")
                for c in t.get("comments", {}).get("nodes", [])
            )
        ]
        if sentinel_unresolved:
            logger.info(f"PR #{pr_number} still has {len(sentinel_unresolved)} unresolved sentinel threads")
            return False

        try:
            pr_reviews = await self.github.get_pr_reviews(pr_number)
            # Latest state per reviewer wins
            reviewer_states: dict[str, str] = {}
            for r in pr_reviews:
                login = r.get("user", {}).get("login", "")
                state = r.get("state", "")
                if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                    reviewer_states[login] = state
            if any(s == "CHANGES_REQUESTED" for s in reviewer_states.values()):
                logger.info(f"PR #{pr_number} has pending CHANGES_REQUESTED review")
                return False
        except Exception as e:
            logger.warning(f"Could not check PR reviews: {e}")

        reviews = await self.db.get_review_logs(pr_number)
        if reviews:
            latest = reviews[-1]
            if latest.get("score", 0) >= config.MIN_ACCEPTABLE_SCORE:
                return True

        return False

    async def notify_ready(self, pr_number: int, topic_title: str):
        """
        Tag the reviewer that the blog is ready to merge, and resolve all remaining
        Sentinel review threads so SwarmOps stops treating them as open action items.
        """
        # Resolve any still-open review threads as a safety net (the agent should have
        # done this via GraphQL curl in PATH B, but we do it here too to be sure).
        sentinel_marker = f"sentinel-review:pr-{pr_number}"
        try:
            threads = await self.github.get_unresolved_review_threads(pr_number)
            for thread in threads:
                thread_id = thread.get("id", "")
                if not thread_id:
                    continue
                # Only resolve threads that contain our sentinel marker
                bodies = [
                    c.get("body", "")
                    for c in thread.get("comments", {}).get("nodes", [])
                ]
                if any(sentinel_marker in b for b in bodies):
                    await self.github.resolve_review_thread(thread_id)
                    logger.info(f"PR #{pr_number}: resolved sentinel thread {thread_id}")
        except Exception as e:
            logger.warning(f"PR #{pr_number}: could not resolve review threads: {e}")

        body = self._strip_emdashes(
            f"@{config.GITHUB_REVIEWER_USERNAME} The blog \"{topic_title}\" has passed all "
            f"editorial reviews, fact-checks, and pricing verifications. "
            f"It is ready for your final review and merge."
        )
        await self.github.add_pr_comment(pr_number, body)
        logger.info(f"Notified @{config.GITHUB_REVIEWER_USERNAME} that PR #{pr_number} is ready")

    # === Review cycle ===

    async def review_pr(self, pr_number: int, run_id: int = None) -> bool:
        """
        Full review cycle for a single PR.
        Creates a git worktree for the PR branch, runs the review, then cleans up.
        Returns True if the blog is ready, False if it needs more work.
        """
        logger.info(f"Starting review of PR #{pr_number}")

        worktree_path, branch = await self.setup_pr_worktree(pr_number)
        if not worktree_path:
            logger.error(f"Failed to create worktree for PR #{pr_number}, aborting review")
            return False

        try:
            return await self._do_review(pr_number, worktree_path, run_id)
        finally:
            self.cleanup_pr_worktree(pr_number)

    async def _do_review(self, pr_number: int, worktree_path: str, run_id: int = None) -> bool:
        """Run the full review with the PR branch checked out in worktree_path."""
        blog_content, blog_files, primary_file_path = await self.get_pr_blog_content(pr_number, worktree_path)
        if not blog_content:
            logger.warning(f"No blog content found in PR #{pr_number}")
            return False

        # Fetch PR data once - used for topic lookup, issue context, and SHA tracking
        pr_data = await self.github.get_pr(pr_number)
        pr_title = pr_data.get("title", f"PR #{pr_number}")
        pr_body = pr_data.get("body", "")
        head_sha = pr_data.get("head", {}).get("sha", "")

        # Fetch linked issue for review context (what the blog was supposed to cover)
        issue_context = ""
        issue_num = None
        issue_match = re.search(r'#(\d+)', pr_body)
        if issue_match:
            issue_num = int(issue_match.group(1))
            try:
                issue_data = await self.github.get_issue(issue_num)
                issue_title = issue_data.get("title", "")
                issue_body = issue_data.get("body", "")
                issue_context = f"Issue #{issue_num}: {issue_title}\n\n{issue_body}"
                logger.info(f"PR #{pr_number}: linked to issue #{issue_num} - {issue_title}")
            except Exception as e:
                logger.warning(f"Could not fetch issue #{issue_num}: {e}")

        # Get or create topic record
        topic = await self.db.get_topic_by_pr(pr_number)
        topic_id = topic["id"] if topic else None

        if not topic_id and issue_num:
            found = await self.db.get_topic_by_issue(issue_num)
            if found:
                topic_id = found["id"]
                await self.db.update_topic_status(topic_id, "pr_created", pr_number=pr_number)

        if not topic_id:
            topic_id = await self.db.create_topic(title=pr_title)
            await self.db.update_topic_status(topic_id, "reviewing", pr_number=pr_number)

        # Derive iteration from GitHub directly - check if a sentinel comment already exists.
        # This is more reliable than the DB counter because DB writes can be skipped on timeout.
        sentinel_marker = f"sentinel-review:pr-{pr_number}"
        sentinel_comment_exists = False
        try:
            existing_comments = await self.github.list_review_comments(pr_number)
            sentinel_comment_exists = any(
                sentinel_marker in c.get("body", "") for c in existing_comments
            )
        except Exception as e:
            logger.warning(f"Could not check existing sentinel comment: {e}")

        db_iteration = await self.db.get_latest_review_iteration(pr_number)
        # If GitHub has our comment but DB shows 0, we must be on iteration >= 2
        if sentinel_comment_exists and db_iteration == 0:
            iteration = 2
        else:
            iteration = db_iteration + 1

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

        await self.db.update_topic_status(topic_id, "reviewing")

        # Single Claude session: fact-check + editorial review + post inline comment
        existing_context = await self.get_existing_blogs_context()

        # Fetch authoritative per-GPU pricing BEFORE spawning Claude so we can
        # pass it directly into the prompt - Claude must not try to fetch pricing itself.
        try:
            pricing_context = await self.pricing.format_pricing_for_prompt()
            logger.info(f"PR #{pr_number}: fetched pricing context ({len(pricing_context)} chars)")
        except Exception as e:
            pricing_context = "Pricing data unavailable - skip pricing verification this iteration."
            logger.warning(f"PR #{pr_number}: could not fetch pricing: {e}")

        # Store recovery context before spawning so a server restart can resume this run
        if run_id:
            await self.db.update_agent_run_recovery_context(run_id, {
                "type": "review",
                "pr_number": pr_number,
                "worktree_path": worktree_path,
                "iteration": iteration,
                "topic_id": topic_id,
                "head_sha": head_sha,
                "phase": "review",
            })

        result = await self.claude.full_pr_review(
            primary_file_path=primary_file_path,
            repo_path=worktree_path,
            github_token=config.GITHUB_TOKEN,
            github_repo=config.GITHUB_REPO,
            pr_number=pr_number,
            existing_blogs_context=existing_context,
            issue_context=issue_context,
            pricing_context=pricing_context,
            iteration=iteration,
            sentinel_marker=sentinel_marker,
            run_id=run_id,
            timeout=config.REVIEW_TIMEOUT_SECONDS,
        )

        return await self._process_review_result(
            pr_number=pr_number,
            topic_id=topic_id,
            iteration=iteration,
            head_sha=head_sha,
            blog_content=blog_content,
            result=result,
        )

    async def _process_review_result(
        self,
        pr_number: int,
        topic_id: int,
        iteration: int,
        head_sha: str,
        blog_content: str,
        result: dict,
    ) -> bool:
        """
        Process the result from a completed Claude review session.
        Shared between normal flow and post-restart recovery.
        Returns True if the blog is ready to merge.
        """
        if not result.get("success"):
            logger.error(f"Review agent failed: {result.get('error')}")
            return False

        # Parse the JSON summary Claude returns at the end
        raw = result.get("result", "")
        if isinstance(raw, dict):
            raw = raw.get("result", "") or raw.get("text", "") or json.dumps(raw)

        review = {}
        try:
            json_match = re.search(r'\{[\s\S]*\}', str(raw))
            if json_match:
                review = json.loads(json_match.group())
                logger.info(
                    f"[review] score={review.get('overall_score')}, "
                    f"improvements={len(review.get('improvements', []))}, "
                    f"comment_posted={review.get('comment_posted')}"
                )
            else:
                logger.error("[review] No JSON summary found in Claude output")
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"[review] Failed to parse result JSON: {e}")

        score = review.get("overall_score", 0)
        improvements = review.get("improvements") or []
        outdated = (review.get("outdated_items") or []) + (review.get("fact_check_flags") or [])
        stale = review.get("stale_data") or []

        # Pricing mismatches are now flagged by Claude using the pre-fetched pricing table.
        # They appear as entries in improvements/outdated with "pricing" in their text.
        pricing_issues = review.get("pricing_issues") or []

        # Broken or mismatched links in the blog are hard blockers
        link_audit = review.get("link_audit") or []
        bad_links = [item for item in link_audit if isinstance(item, dict) and item.get("verdict") in ("BROKEN", "MISMATCH")]

        has_issues = (
            len(improvements) > 0
            or len(outdated) > 0
            or len(stale) > 0
            or len(pricing_issues) > 0
            or len(bad_links) > 0
        )
        logger.info(
            f"PR #{pr_number} decision: score={score}, has_issues={has_issues}, "
            f"improvements={len(improvements)}, outdated={len(outdated)}, "
            f"stale={len(stale)}, pricing_issues={len(pricing_issues)}, "
            f"bad_links={len(bad_links)} (broken/mismatch out of {len(link_audit)} audited)"
        )

        await self.db.create_review_log(
            topic_id=topic_id,
            pr_number=pr_number,
            iteration=iteration,
            review_type="full",
            score=score,
            review_data={"review": review, "pricing_issues": pricing_issues, "head_sha": head_sha, "link_audit": link_audit, "bad_links": bad_links},
        )
        await self.db.update_topic_status(topic_id, "reviewing",
                                          review_score=score,
                                          review_iterations=iteration)

        if has_issues or score < config.MIN_ACCEPTABLE_SCORE:
            logger.info(f"PR #{pr_number} iteration {iteration}: score={score}/10, needs work")
            return False
        else:
            topic_rec = await self.db.get_topic(topic_id)
            title = topic_rec.get("title", f"PR #{pr_number}") if topic_rec else f"PR #{pr_number}"
            # Mark ready FIRST so the next poll doesn't re-trigger notify_ready if
            # notify_ready is slow or partially fails.
            await self.db.update_topic_status(topic_id, "ready")
            try:
                await self.notify_ready(pr_number, title)
            except Exception as e:
                # Status is already "ready" so the blog won't be re-reviewed, but the
                # human reviewer was not tagged. Log prominently for manual follow-up.
                logger.error(
                    f"PR #{pr_number}: notify_ready failed after marking blog ready - "
                    f"@{config.GITHUB_REVIEWER_USERNAME} must be tagged manually. Error: {e}"
                )
            return True

    # === Polling ===

    async def poll_prs(self):
        """
        Poll for PRs labeled 'blog' and review new/updated ones.

        Two-phase execution:
          Phase 1 (sequential): check each PR's SHA, seed _tracked_prs, build review queue.
          Phase 2 (concurrent): run up to MAX_CONCURRENT_REVIEWS reviews in parallel using
                                 asyncio.Semaphore so each PR gets its own Claude subprocess.
        """
        logger.info("Polling for blog PRs...")

        await self.ensure_repo_cloned()

        prs = await self.github.list_prs(state="open", labels=config.GITHUB_BLOG_LABEL)
        logger.info(f"Found {len(prs)} open PRs with '{config.GITHUB_BLOG_LABEL}' label")

        if not prs:
            logger.info("No open blog PRs found - nothing to review")
            return

        # ── Phase 1: sequential SHA check ────────────────────────────────────
        # Fast path (no Claude). Build list of PRs that need a full review.
        review_queue: list[tuple[int, str]] = []  # (pr_number, pr_title)

        for pr_item in prs:
            pr_number = pr_item.get("number")
            pr_title = pr_item.get("title", "untitled")
            if not pr_number:
                continue

            try:
                pr_data = await self.github.get_pr(pr_number)
            except Exception as e:
                logger.error(f"Failed to get PR #{pr_number}: {e}")
                continue

            latest_sha = pr_data.get("head", {}).get("sha", "")
            last_seen = self._tracked_prs.get(pr_number, "")

            # On first poll after a restart, seed _tracked_prs with the SHA that was
            # last reviewed (stored in review_data), not the current live SHA.
            # This ensures new commits pushed while the service was down are still reviewed.
            if not last_seen:
                prev_logs = await self.db.get_review_logs(pr_number)
                if prev_logs:
                    try:
                        last_review_data = json.loads(prev_logs[-1].get("review_data", "{}") or "{}")
                        last_reviewed_sha = last_review_data.get("head_sha", "")
                    except (json.JSONDecodeError, TypeError):
                        last_reviewed_sha = ""
                    if last_reviewed_sha:
                        self._tracked_prs[pr_number] = last_reviewed_sha
                        last_seen = last_reviewed_sha
                        logger.info(
                            f"PR #{pr_number}: seeded from DB (last reviewed sha={last_reviewed_sha[:7]}, "
                            f"current sha={latest_sha[:7]})"
                        )

            if latest_sha == last_seen:
                logger.info(f"PR #{pr_number} ({pr_title}): no new commits, checking if ready...")
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

            if last_seen:
                logger.info(
                    f"PR #{pr_number} ({pr_title}): new commits "
                    f"({last_seen[:7]} -> {latest_sha[:7]}), queuing review..."
                )
            else:
                logger.info(
                    f"PR #{pr_number} ({pr_title}): first time seeing this PR "
                    f"(sha: {latest_sha[:7]}), queuing review..."
                )

            # Mark SHA NOW before dispatch so the next scheduled poll (which may fire
            # while this review is still running) does not re-queue the same commit.
            self._tracked_prs[pr_number] = latest_sha
            review_queue.append((pr_number, pr_title))

        if not review_queue:
            return

        # ── Phase 2: concurrent reviews ───────────────────────────────────────
        concurrency = max(1, config.MAX_CONCURRENT_REVIEWS)
        logger.info(
            f"Dispatching {len(review_queue)} review(s) "
            f"(concurrency={concurrency}): PRs {[n for n, _ in review_queue]}"
        )

        semaphore = asyncio.Semaphore(concurrency)

        async def _run_review(pr_number: int, pr_title: str):
            async with semaphore:
                run_id = await self.db.create_agent_run("reviewer", None)
                # Store pr_number immediately so it shows while the run is still running
                await self.db.set_agent_run_pr(run_id, pr_number)
                try:
                    is_ready = await self.review_pr(pr_number, run_id=run_id)
                    status = "ready" if is_ready else "needs work"
                    logger.info(f"PR #{pr_number} ({pr_title}): review complete - {status}")
                    await self.db.finish_agent_run(
                        run_id, "completed",
                        {"pr_number": pr_number, "ready": is_ready},
                    )
                except Exception as e:
                    logger.error(
                        f"Review failed for PR #{pr_number} ({pr_title}): {e}", exc_info=True
                    )
                    await self.db.finish_agent_run(run_id, "error", error=str(e))

        await asyncio.gather(*[_run_review(n, t) for n, t in review_queue])

    async def run_continuous(self):
        """Run the review agent continuously, polling at configured intervals."""
        logger.info(f"Starting review agent (poll interval: {config.PR_POLL_INTERVAL_SECONDS}s)")
        while True:
            try:
                await self.poll_prs()
            except Exception as e:
                logger.error(f"Review poll error: {e}")
            await asyncio.sleep(config.PR_POLL_INTERVAL_SECONDS)
