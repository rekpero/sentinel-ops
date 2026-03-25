"""
Claude Code CLI Service - spawns Claude Code instances for AI-powered
blog review, fact-checking, and topic discovery.

Uses --output-format stream-json to stream events in real-time and
store them in the database for live log viewing in the dashboard.
"""
import asyncio
import json
import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from backend.services.stream_parser import parse_stream_line

if TYPE_CHECKING:
    from backend.db.database import Database

logger = logging.getLogger(__name__)


class ClaudeService:
    def __init__(
        self,
        claude_cmd: str = "claude",
        setup_token: str = "",
        workdir: str = "",
        db: "Database | None" = None,
    ):
        self.claude_cmd = claude_cmd
        self.setup_token = setup_token
        self.workdir = workdir
        self.db = db

    def _build_env(self) -> dict:
        """Build environment variables for Claude Code subprocess."""
        env = os.environ.copy()
        if self.setup_token:
            env["CLAUDE_SETUP_TOKEN"] = self.setup_token
        return env

    async def run_prompt(
        self,
        prompt: str,
        workdir: str = "",
        allowed_tools: list[str] = None,
        run_id: int = None,
        phase: str = "general",
        timeout: int = 600,
    ) -> dict:
        """
        Run a prompt through Claude Code CLI.
        Spawns with start_new_session=True so the subprocess survives a server restart.
        stdout is redirected to a log file; a tail loop reads events asynchronously.
        PID and log_path are stored in the DB immediately after spawn.
        """
        cwd = workdir or self.workdir

        # DO NOT add --max-turns here. Claude Code must be allowed to run as many turns
        # as needed to complete the review (fact-check + link audit + editorial + GitHub
        # API calls). Artificially capping turns causes the session to terminate before
        # STEP 4/5, producing no review comment and a silent error. Use the timeout
        # parameter to bound wall-clock time instead.
        cmd = [
            self.claude_cmd,
            "--print",
            "--output-format", "stream-json",
            "--verbose",
        ]
        if allowed_tools:
            for tool in allowed_tools:
                cmd.extend(["--allowedTools", tool])

        logger.info(f"Spawning Claude Code (phase={phase}, run_id={run_id}) in {cwd}: {prompt[:100]}...")

        # Create log file when run_id is available
        log_path = None
        if run_id is not None and self.workdir:
            log_dir = Path(self.workdir) / "runs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = str(log_dir / f"{run_id}-{phase}.log")

        try:
            if log_path:
                log_file = open(log_path, "w")
                try:
                    process = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=log_file,
                        stderr=subprocess.PIPE,
                        cwd=cwd or None,
                        env=self._build_env(),
                        start_new_session=True,  # Survive server restart
                        text=True,
                    )
                finally:
                    log_file.close()  # Close our FD; child keeps its own copy

                # Write prompt to stdin and always close it
                try:
                    process.stdin.write(prompt)
                except BrokenPipeError:
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except Exception:
                        pass

                # Persist PID + log_path immediately so recovery can find this run
                if self.db is not None and run_id is not None:
                    await self.db.update_agent_run_process(run_id, process.pid, log_path)

                return await self._tail_log_async(
                    log_path=log_path,
                    pid=process.pid,
                    run_id=run_id,
                    phase=phase,
                    timeout=timeout,
                )
            else:
                # Fallback: pipe-based when no run_id/workdir (e.g. discovery)
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd or None,
                    env=self._build_env(),
                )
                process.stdin.write(prompt.encode())
                await process.stdin.drain()
                process.stdin.close()

                final_result = None
                stderr_data = b""

                async def read_stdout():
                    nonlocal final_result
                    async for raw_line in process.stdout:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        event = parse_stream_line(line)
                        if event is None:
                            continue
                        if event["event_type"] == "result":
                            final_result = json.loads(line)

                async def read_stderr():
                    nonlocal stderr_data
                    stderr_data = await process.stderr.read()

                try:
                    await asyncio.wait_for(
                        asyncio.gather(read_stdout(), read_stderr()),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    return {"success": False, "error": f"Timeout after {timeout} seconds"}

                await process.wait()
                if process.returncode != 0:
                    stderr_text = stderr_data.decode("utf-8", errors="replace")
                    return {"success": False, "error": stderr_text[:1000], "exit_code": process.returncode}
                if final_result is not None:
                    return {"success": True, "result": final_result}
                return {"success": False, "error": "No result event in Claude Code output"}

        except Exception as e:
            logger.error(f"Claude Code error: {e}")
            return {"success": False, "error": str(e)}

    async def _tail_log_async(
        self,
        log_path: str,
        pid: int,
        run_id: int = None,
        phase: str = "general",
        start_offset: int = 0,
        timeout: int = 600,
    ) -> dict:
        """
        Tail a log file written by a detached Claude subprocess.
        Polls for new lines, parses stream-json events, persists byte offset for restart recovery.
        """
        start_time = asyncio.get_event_loop().time()
        final_result = None
        lines_since_offset_save = 0

        # Wait briefly for the log file to appear
        for _ in range(50):
            if os.path.exists(log_path):
                break
            await asyncio.sleep(0.1)

        try:
            with open(log_path, "r", errors="replace") as f:
                if start_offset > 0:
                    f.seek(start_offset)
                    # Scan existing data first - the result event may already be in the log
                    # (process finished before/during server restart). This also avoids
                    # falsely treating a PID-reused process as our still-running subprocess.
                    for existing_line in f:
                        stripped = existing_line.strip()
                        if not stripped:
                            continue
                        event = parse_stream_line(stripped)
                        if event is not None:
                            if run_id is not None and self.db is not None:
                                try:
                                    await self.db.insert_run_event(
                                        run_id=run_id,
                                        phase=phase,
                                        event_type=event["event_type"],
                                        event_data=stripped,
                                    )
                                except Exception:
                                    pass
                            if event["event_type"] == "result":
                                final_result = json.loads(stripped)
                    if final_result is not None:
                        # Result already in log - no need to tail further
                        if run_id is not None and self.db is not None:
                            try:
                                await self.db.update_agent_run_log_offset(run_id, f.tell())
                            except Exception:
                                pass
                        return {"success": True, "result": final_result}

                while True:
                    line = f.readline()
                    if line:
                        stripped = line.strip()
                        if stripped:
                            event = parse_stream_line(stripped)
                            if event is not None:
                                if run_id is not None and self.db is not None:
                                    try:
                                        await self.db.insert_run_event(
                                            run_id=run_id,
                                            phase=phase,
                                            event_type=event["event_type"],
                                            event_data=stripped,
                                        )
                                    except Exception as e:
                                        logger.warning(f"Failed to store run event: {e}")
                                if event["event_type"] == "result":
                                    final_result = json.loads(stripped)
                                    break  # Result found - no need to wait for process to die
                        # Persist offset every 20 lines
                        lines_since_offset_save += 1
                        if lines_since_offset_save >= 20:
                            lines_since_offset_save = 0
                            if run_id is not None and self.db is not None:
                                try:
                                    await self.db.update_agent_run_log_offset(run_id, f.tell())
                                except Exception as e:
                                    logger.warning(f"Failed to update log offset: {e}")
                    else:
                        # No new data - check if process still alive
                        try:
                            os.kill(pid, 0)
                        except (OSError, ProcessLookupError):
                            # Process finished - drain any remaining lines
                            for remaining in f:
                                remaining = remaining.strip()
                                if not remaining:
                                    continue
                                event = parse_stream_line(remaining)
                                if event is not None:
                                    if run_id is not None and self.db is not None:
                                        try:
                                            await self.db.insert_run_event(
                                                run_id=run_id,
                                                phase=phase,
                                                event_type=event["event_type"],
                                                event_data=remaining,
                                            )
                                        except Exception as e:
                                            logger.warning(f"Failed to store run event: {e}")
                                    if event["event_type"] == "result":
                                        final_result = json.loads(remaining)
                            # Save final offset
                            if run_id is not None and self.db is not None:
                                try:
                                    await self.db.update_agent_run_log_offset(run_id, f.tell())
                                except Exception as e:
                                    logger.warning(f"Failed to save final log offset: {e}")
                            break

                        elapsed = asyncio.get_event_loop().time() - start_time
                        if elapsed > timeout:
                            logger.error(f"Claude Code timed out after {timeout}s (pid={pid})")
                            try:
                                os.kill(pid, signal.SIGTERM)
                            except Exception:
                                pass
                            return {"success": False, "error": f"Timeout after {timeout} seconds"}

                        # Save offset on idle and yield to event loop
                        if run_id is not None and self.db is not None:
                            try:
                                await self.db.update_agent_run_log_offset(run_id, f.tell())
                            except Exception as e:
                                logger.warning(f"Failed to update idle log offset: {e}")
                        await asyncio.sleep(0.3)

        except asyncio.CancelledError:
            # Task was cancelled (e.g. superseded by a new commit). Kill the subprocess
            # so it doesn't become an orphan posting stale review comments.
            # pgid == pid because Claude was spawned with start_new_session=True.
            try:
                os.killpg(pid, signal.SIGTERM)
                logger.info(f"Tail cancelled - sent SIGTERM to Claude process group (pid={pid})")
            except (ProcessLookupError, OSError):
                pass
            raise
        except Exception as e:
            logger.error(f"Log tail error (pid={pid}, log={log_path}): {e}")
            return {"success": False, "error": str(e)}

        if final_result is not None:
            return {"success": True, "result": final_result}
        return {"success": False, "error": "No result event in Claude Code output"}

    async def reattach_run(
        self,
        run_id: int,
        pid: int,
        log_path: str,
        log_offset: int = 0,
        phase: str = "review",
    ) -> dict:
        """
        Reattach to a Claude subprocess that survived a server restart.
        Tails the log file from the stored byte offset and returns the result.
        """
        logger.info(f"Reattaching run_id={run_id} pid={pid} offset={log_offset} log={log_path}")
        return await self._tail_log_async(
            log_path=log_path,
            pid=pid,
            run_id=run_id,
            phase=phase,
            start_offset=log_offset,
        )

    async def full_pr_review(
        self,
        primary_file_path: str,
        repo_path: str,
        github_token: str,
        github_repo: str,
        pr_number: int,
        existing_blogs_context: str = "",
        issue_context: str = "",
        pricing_context: str = "",
        iteration: int = 1,
        sentinel_marker: str = "",
        run_id: int = None,
        timeout: int = 1200,
    ) -> dict:
        """
        Single multi-step Claude Code session that:
          1. Reads the primary blog file
          2. Fact-checks all claims via web search
          3. Performs editorial review and scoring
          4. Posts/updates a resolvable inline PR review comment via GitHub API (curl)
          5. Returns a JSON summary for database storage

        sentinel_marker is embedded as an invisible HTML comment in every review body so
        we can always locate our own comment without relying on username or stored IDs.
        """
        issue_section = ""
        if issue_context:
            issue_section = f"""
---
## ORIGINAL ISSUE BRIEF

This is what the blog was commissioned to cover. Use it to evaluate whether the blog
actually addresses the topic, angle, and requirements that were specified:

{issue_context[:3000]}
"""

        marker_tag = f"<!-- {sentinel_marker} -->" if sentinel_marker else f"<!-- sentinel-review:pr-{pr_number} -->"

        owner, repo_name = github_repo.split("/", 1)

        if iteration == 1:
            step4b = "(This is iteration 1 - skip this step, there is no previous comment to resolve.)"
        else:
            step4b = (
                f"Fetch ALL unresolved Sentinel review threads and evaluate each one against the current blog.\n"
                f"\n"
                f"First, fetch all unresolved review threads via GraphQL:\n"
                f"```\n"
                f"curl -s -X POST \\\\\n"
                f"  -H \"Authorization: token {github_token}\" \\\\\n"
                f"  -H \"Content-Type: application/json\" \\\\\n"
                f"  https://api.github.com/graphql \\\\\n"
                f"  -d '{{\"query\": \"query {{ repository(owner: \\\\\"{owner}\\\\\", name: \\\\\"{repo_name}\\\\\") {{ pullRequest(number: {pr_number}) {{ reviewThreads(first: 100) {{ nodes {{ id isResolved comments(first: 10) {{ nodes {{ body }} }} }} }} }} }} }}\"}}'  \n"
                f"```\n"
                f"\n"
                f"From the response, collect ALL unresolved threads (`isResolved: false`) whose comments contain `{marker_tag}`.\n"
                f"There may be multiple such threads from previous review iterations - process ALL of them.\n"
                f"\n"
                f"For EACH unresolved Sentinel thread found:\n"
                f"1. Read the full comment body to understand the exact issue(s) it was flagging\n"
                f"2. Cross-reference with the current blog content you already read in STEP 1 to determine whether each specific issue has been addressed\n"
                f"3. If the issue IS fixed in the current version of the blog, resolve that thread:\n"
                f"```\n"
                f"curl -s -X POST \\\\\n"
                f"  -H \"Authorization: token {github_token}\" \\\\\n"
                f"  -H \"Content-Type: application/json\" \\\\\n"
                f"  https://api.github.com/graphql \\\\\n"
                f"  -d '{{\"query\": \"mutation {{ resolveReviewThread(input: {{threadId: \\\\\"<thread-id>\\\\\"}}) {{ thread {{ id isResolved }} }} }}\"}}'  \n"
                f"```\n"
                f"4. If the issue is NOT fixed, leave it unresolved - it will carry forward as a remaining issue\n"
                f"\n"
                f"After processing all threads, compile:\n"
                f"- **resolved_threads**: threads you just resolved (their issues were fixed)\n"
                f"- **still_unresolved_issues**: issues from unresolved threads that were NOT fixed\n"
                f"- **new_issues**: new issues found in STEP 3 that did not appear in any previous comment\n"
                f"\n"
                f"The combined list of `still_unresolved_issues` + `new_issues` is what determines PATH A vs PATH B in STEP 4C.\n"
                f"If the GraphQL call errors on any individual thread, log it and continue with the rest."
            )

        step4 = f"""## STEP 4 - SUBMIT REVIEW

This is review iteration {iteration}. Follow ALL sub-steps below in order.

---
### STEP 4A - Resolve previous Sentinel threads (skip on iteration 1)

{step4b}

---
### STEP 4B - Submit the combined review via the PR reviews API

Get the PR head SHA and a valid diff line number for the primary blog file:
```
curl -s -H "Authorization: token {github_token}" \\
  -H "Accept: application/vnd.github.v3+json" \\
  https://api.github.com/repos/{github_repo}/pulls/{pr_number}
```
Extract `head.sha`. Then:
```
curl -s -H "Authorization: token {github_token}" \\
  -H "Accept: application/vnd.github.v3+json" \\
  "https://api.github.com/repos/{github_repo}/pulls/{pr_number}/files"
```
Find the primary blog file entry. Parse its `patch` field: use the START number from `@@ -N,N +START,COUNT @@`
or any `+` line. If you get a 422, try line 2, 5, or 10.

Write the review JSON to a temp file (avoids shell escaping issues), then post it with curl:

```python
python3 << 'PYEOF'
import json

# PATH A: issues still remain - include inline comment with action items
review = {{
    "commit_id": "<head.sha>",
    "body": "### Sentinel Review - Iteration {iteration}\\n\\n**Overall score: <X>/10**\\n\\n| Category | Score |\\n|---|---|\\n| Content Quality | <X>/10 |\\n| SEO Optimization | <X>/10 |\\n| Technical Accuracy | <X>/10 |\\n| Readability | <X>/10 |\\n| Internal Linking | <X>/10 |\\n\\n**Summary:** <one paragraph general assessment>\\n\\n**Fact-check:** <what was verified, any outstanding concerns>\\n\\n**Link audit:** <total links checked, count of VALID/BROKEN/MISMATCH/LOW_VALUE>\\n\\n**Previous issues resolved:** <count resolved in 4A, or N/A for iteration 1>\\n\\nAction items posted as inline comment below.",
    "event": "COMMENT",
    "comments": [
        {{
            "path": "<relative file path>",
            "line": <valid line number>,
            "side": "RIGHT",
            "body": "<ONLY the remaining action items: still_unresolved_issues + new_issues. List each one clearly with severity. No scores here.>\\n\\n{marker_tag}"
        }}
    ]
}}

# PATH B: all issues resolved - omit comments array
# review = {{
#     "commit_id": "<head.sha>",
#     "body": "### Sentinel Review - Iteration {iteration}\\n\\n**Overall score: <X>/10**\\n\\n...scorecard...\\n\\nAll issues resolved - blog is ready to merge.",
#     "event": "COMMENT"
# }}

with open("/tmp/sentinel_review_{pr_number}.json", "w") as f:
    json.dump(review, f)
PYEOF

curl -s -X POST \\
  -H "Authorization: token {github_token}" \\
  -H "Accept: application/vnd.github.v3+json" \\
  -H "Content-Type: application/json" \\
  "https://api.github.com/repos/{github_repo}/pulls/{pr_number}/reviews" \\
  -d "@/tmp/sentinel_review_{pr_number}.json"
```

**PATH A - Issues still remain** (score < 8 OR high/medium severity remaining issues OR BROKEN/MISMATCH links):
- Use the PATH A review dict (with `"comments"` array)
- The inline comment body must end with `{marker_tag}` on its own line
- The review `"body"` contains the scorecard summary only - action items go in the inline comment

**PATH B - All issues resolved** (score >= 8 AND no remaining issues from 4A AND no new high/medium issues AND no BROKEN/MISMATCH links):
- Use the PATH B review dict (commented out above) - no `"comments"` key
- The review `"body"` summary ends with "All issues resolved - blog is ready to merge."

IMPORTANT: Use ONLY the `/pulls/{pr_number}/reviews` endpoint (single batched call). Do NOT post separately to `/issues/{pr_number}/comments` or `/pulls/{pr_number}/comments`. The review body and inline comment body must NOT contain emdashes. Use hyphens only."""

        prompt = f"""You are a senior blog reviewer for Spheron, a GPU cloud infrastructure company.
Complete ALL of the following steps in order. Do not skip any step.
{issue_section}
---
## STEP 1 - READ THE BLOG

Read the primary blog file:
  {primary_file_path}

This is the ONLY blog you are reviewing. Do not read any other blog files.

---
## STEP 2 - FACT CHECK

Using WebSearch and WebFetch, verify every technical claim in the blog:
- GPU specs (VRAM, bandwidth, TFLOPS) - verify against official sources
- Model names, versions, release dates
- Third-party tool and library claims
- Any benchmarks or performance comparisons
- Flag anything outdated or incorrect

---
## STEP 2B - AUDIT ALL EXISTING LINKS IN THE BLOG

Extract every hyperlink already present in the blog (markdown `[text](url)` or HTML `<a href>`).
For EACH link, use WebFetch to fetch the URL and evaluate:

1. **Existence**: Does the page return a valid response (not 404, not redirect to homepage)?
2. **Primary content match**: Is the page's PRIMARY topic directly about the concept being discussed at the point of the link in the blog? Apply the strict test: "If a reader clicks expecting to learn more about [concept], does this page deliver a focused explanation of that exact concept?" Incidental mentions don't count.
3. **Verdict**: Assign one of:
   - `VALID` - exists and primary content matches context
   - `BROKEN` - 404 or unreachable
   - `MISMATCH` - page exists but primary content does not match the link context (e.g. a billing page linked in a spot-instances section, a general overview linked for a specific API call)
   - `LOW_VALUE` - page exists and loosely related, but the link adds no meaningful depth for the reader at that point

Report ALL links with their verdict and a brief reason. Do not skip any link.

---
## STEP 2C - PRICING VERIFICATION

The following per-GPU prices were fetched live from the Spheron API immediately before this review.
Do NOT use WebSearch or WebFetch to look up pricing - use ONLY this table:

{pricing_context}

Check every GPU price mentioned in the blog against this table:
- If a price in the blog does not match the table, flag it as a pricing issue with the correct value.
- Prices in the blog should be per-GPU per hour. If the blog states a cluster price (multiple GPUs), flag it.
- Verify the blog includes the disclaimer that pricing fluctuates based on GPU availability.
- Report all pricing issues in the `pricing_issues` array of your final JSON output.

---
## STEP 3 - EDITORIAL REVIEW

Score the blog 1-10 on each category and identify specific improvements:
- Content Quality: depth, accuracy, completeness - **does it cover what the issue brief asked for?**
- SEO Optimization: keywords, title, meta, structure
- Technical Accuracy: all technical claims verified (cross-reference Step 2)
- Readability: flow, clarity, formatting, sentence length - **including link density (see below)**
- Internal Linking: links to docs.spheron.ai and other Spheron blog posts

If an original issue brief was provided above, check:
- Does the blog cover the topic and angle specified in the issue?
- Are the target keywords from the issue incorporated?
- Does it follow the outlined structure or deviate without good reason?
- Flag any missing sections or requirements from the brief as high-severity improvements.

LINK DENSITY CHECK (part of Readability score):
- Count the total number of links in the blog and note any paragraphs with 3 or more links.
- Over-linking harms readability and dilutes link value. Flag if:
  - Total links exceed 15 for a standard blog post
  - Any single paragraph has 3 or more links
  - Multiple consecutive sentences each contain a link
- For each over-linked area, identify which specific links are LOW_VALUE or MISMATCH (from Step 2B) and recommend removing them. Only keep links that genuinely help the reader understand or act on the content.

EXISTING SPHERON BLOGS (for cross-linking suggestions):
{existing_blogs_context[:3000]}

RULES:
- Never use emdashes (-- or unicode em dash). Use hyphens (-) instead.
- Cross-link to docs.spheron.ai where technical concepts are mentioned
- Cross-link to existing Spheron blog posts listed above where relevant

STRICT LINK VERIFICATION RULES (CRITICAL - violations cause real harm):
- NEVER suggest a specific URL in your review unless you have personally fetched it with WebFetch and confirmed BOTH: (a) the page exists and returns a valid response, AND (b) the page's PRIMARY content directly explains the specific concept being discussed at the point of the link.
- "Primary content" test: ask yourself "If a reader clicks this link expecting to learn more about [concept], does this page deliver a focused explanation of that concept?" If the answer is anything less than a clear YES, do NOT suggest the link.
- A page that incidentally mentions a keyword while being primarily about something else is NOT valid. Examples of bad matches: a billing/pricing page linked in a spot-instances technical section (billing is the topic, not spot instance mechanics); a general overview page linked for a specific API; a blog about a different product that mentions the keyword in passing.
- If you want to suggest a link but have NOT verified it with WebFetch, you MUST flag it as unverified using this exact format: "NOTE TO AGENT: Before adding this link, use WebFetch to fetch [URL] and confirm (1) the page exists and (2) its primary content directly explains [specific concept]. Only add the link if confirmed."
- Do NOT fabricate or guess doc URLs. If you are not certain a page exists at a specific URL, do not suggest that URL at all - describe the concept/topic to link to and let the agent find the right URL.
- Existing Spheron blog slugs listed above are candidates only - you MUST still WebFetch them to confirm the blog's actual topic matches the specific context before suggesting them as cross-links.
- The writing agent trusts your review completely and will add every link you suggest without question. A wrong, broken, or contextually mismatched link will go straight into the published blog and confuse readers.

---
{step4}

---
## STEP 5 - RETURN JSON SUMMARY (CRITICAL - THIS MUST BE YOUR VERY LAST OUTPUT)

After posting the comment, output a JSON object with this exact structure.
For `comment_id`: use the numeric `id` of the NEW inline comment posted in step 4C (PATH A only).
If PATH B was taken (no inline comment created), set `comment_id` to null.

IMPORTANT: The JSON block below MUST be your FINAL output. Do NOT write any text, commentary,
or follow-up analysis after the JSON. The system parses your last message to extract this JSON.
If you output anything after it, the review score will not be recorded and the pipeline will break.

{{
  "overall_score": 7.5,
  "scores": {{
    "content_quality": 8,
    "seo_optimization": 7,
    "technical_accuracy": 8,
    "readability": 8,
    "internal_linking": 6
  }},
  "summary": "One paragraph summary of the review",
  "improvements": [
    {{
      "type": "content|seo|technical|linking|style",
      "severity": "high|medium|low",
      "description": "What needs to change",
      "suggestion": "Specific suggestion"
    }}
  ],
  "fact_check_flags": [
    {{
      "claim": "The specific claim",
      "concern": "Why it might be wrong",
      "suggestion": "What to verify or update"
    }}
  ],
  "outdated_items": [
    {{
      "claim": "The outdated claim",
      "current_info": "What is accurate now",
      "source": "URL"
    }}
  ],
  "stale_data": [],
  "pricing_issues": [
    {{
      "gpu": "GPU type mentioned in blog",
      "mentioned_price": 2.50,
      "correct_price": 2.01,
      "price_type": "ondemand|spot",
      "description": "Blog says $2.50/hr but current on-demand price is $2.01/hr per GPU"
    }}
  ],
  "link_audit": [
    {{
      "url": "https://example.com",
      "anchor_text": "link text from blog",
      "verdict": "VALID|BROKEN|MISMATCH|LOW_VALUE",
      "reason": "Brief explanation of verdict"
    }}
  ],
  "link_density_issue": false,
  "comment_posted": true,
  "comment_id": 123456789
}}
"""
        return await self.run_prompt(
            prompt,
            workdir=repo_path,
            allowed_tools=["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Bash"],
            run_id=run_id,
            phase="review",
            timeout=timeout,
        )

    async def review_blog(
        self,
        blog_content: str,
        repo_path: str,
        primary_file_path: str = "",
        existing_blogs_context: str = "",
        run_id: int = None,
        phase: str = "editorial",
    ) -> dict:
        """
        Use Claude Code to perform an editorial review of a blog post.
        Runs inside the cloned repo for full context.
        """
        file_instruction = ""
        if primary_file_path:
            file_instruction = f"""
PRIMARY BLOG FILE TO REVIEW: {primary_file_path}
Read this specific file first using the Read tool. This is the ONLY blog you should review.
Do NOT read or review other blog files found in the repository - those are existing posts, not the one being added.
"""

        prompt = f"""You are an expert blog editor and SEO specialist for a GPU cloud infrastructure company (Spheron).
Review the blog post and provide a detailed editorial review.
{file_instruction}
IMPORTANT RULES:
- Never use emdashes (--) anywhere in your review or suggestions. Use hyphens (-) instead.
- Score the blog from 1-10 on: Content Quality, SEO Optimization, Technical Accuracy, Readability, Internal Linking
- Check for proper cross-links to docs.spheron.ai where technical concepts are mentioned
- Check for proper cross-links to other existing blog posts on spheron.network/blog/
- Verify the blog follows the style and tone of existing blogs in this repository
- Check that all claims are factually accurate and up-to-date
- Look for any outdated data, stale references, or incorrect information

STRICT LINK VERIFICATION RULES (CRITICAL):
- NEVER suggest a specific URL unless you have fetched it with WebFetch and confirmed: (a) the page exists, AND (b) the page's PRIMARY content directly explains the specific concept being discussed at the point of the link - not just incidentally mentions it.
- Primary content test: "If a reader clicks expecting to learn more about [concept], does this page deliver a focused explanation of that concept?" Anything less than a clear YES = do not suggest.
- A billing page is not valid for a technical spot-instances section. A general overview is not valid for a specific API reference. Incidental keyword mentions don't count.
- If you cannot verify a link with WebFetch, flag it as: "NOTE TO AGENT: Before adding this link, use WebFetch to fetch [URL] and confirm (1) it exists and (2) its primary content directly explains [specific concept]. Only add if confirmed."
- Do NOT guess or fabricate doc/blog URLs. Describe the concept to link to and let the agent find the right URL.
- The writing agent trusts your review completely - every link you suggest must be verified or flagged.

EXISTING BLOGS CONTEXT (for internal linking suggestions):
{existing_blogs_context[:3000]}

BLOG CONTENT (for reference - read the file above for the canonical version):
{blog_content[:5000]}

Provide your review as a JSON object with this structure:
{{
  "overall_score": 8,
  "scores": {{
    "content_quality": 8,
    "seo_optimization": 7,
    "technical_accuracy": 9,
    "readability": 8,
    "internal_linking": 6
  }},
  "summary": "Brief overall assessment",
  "improvements": [
    {{
      "type": "content|seo|technical|linking|style",
      "severity": "high|medium|low",
      "description": "What needs to change",
      "suggestion": "Specific suggestion"
    }}
  ],
  "fact_check_flags": [
    {{
      "claim": "The specific claim in the blog",
      "concern": "Why this might be outdated or wrong",
      "suggestion": "What to verify or update"
    }}
  ],
  "missing_crosslinks": [
    {{
      "context": "Where in the blog",
      "suggested_link": "URL to link to",
      "anchor_text": "Suggested anchor text"
    }}
  ]
}}
"""
        return await self.run_prompt(
            prompt,
            workdir=repo_path,
            allowed_tools=["Read", "Glob", "Grep", "WebFetch", "WebSearch"],
            run_id=run_id,
            phase=phase,
        )

    async def discover_topics(self, existing_topics: list[str], focus_areas: str, topic_count: int = 3, run_id: int = None) -> dict:
        """
        Use Claude Code to discover trending blog topics for SEO.
        """
        existing_str = "\n".join(f"- {t}" for t in existing_topics)
        prompt = f"""You are an expert SEO content strategist for Spheron, a GPU cloud infrastructure company.
Your job is to find {topic_count} blog topics that will rank well in search and drive GPU cloud users to Spheron.

FOCUS AREAS:
{focus_areas}

EXISTING BLOG TOPICS (avoid duplicates):
{existing_str}

RESEARCH INSTRUCTIONS:
1. Search for trending topics in AI/ML infrastructure, GPU cloud, LLM deployment
2. Look at competitor blogs (RunPod, Vast.ai, Lambda Labs, CoreWeave) for gaps we can fill
3. Check Google Trends and recent AI news for timely topics
4. Find high-volume keywords with reasonable competition
5. Prioritize topics where Spheron's GPU cloud can be positioned as the solution

IMPORTANT: Never use emdashes (--) in any content. Use hyphens (-) instead.

Return a JSON array of exactly {topic_count} topics:
[
  {{
    "title": "Blog post title (SEO optimized)",
    "target_keywords": ["keyword1", "keyword2", "keyword3"],
    "search_volume_estimate": "high|medium|low",
    "competition_level": "high|medium|low",
    "why_it_ranks": "Brief explanation of why this topic will rank well",
    "outline": ["Section 1", "Section 2", "Section 3"],
    "spheron_angle": "How to position Spheron in this content",
    "timeliness": "Why publish this now"
  }}
]
"""
        return await self.run_prompt(
            prompt,
            allowed_tools=["WebFetch", "WebSearch"],
            run_id=run_id,
            phase="discovery",
        )

    async def fact_check_blog(
        self,
        blog_content: str,
        repo_path: str,
        primary_file_path: str = "",
        run_id: int = None,
        phase: str = "fact_check",
    ) -> dict:
        """
        Use Claude Code to fact-check a blog post with web search.
        """
        file_instruction = ""
        if primary_file_path:
            file_instruction = f"""
PRIMARY BLOG FILE TO FACT-CHECK: {primary_file_path}
Read this specific file first using the Read tool. This is the ONLY blog you should fact-check.
Do NOT read or fact-check other blog files - those are existing posts, not the one being reviewed.
"""

        prompt = f"""You are a fact-checker for a technical blog about GPU cloud infrastructure.
Thoroughly fact-check the blog post using web search to verify every claim.
{file_instruction}
IMPORTANT RULES:
- Never use emdashes (--) in any content. Use hyphens (-) instead.
- Search for each technical claim, statistic, benchmark, and comparison
- Verify model names, versions, and release dates are current
- Check that referenced tools, libraries, and frameworks are still active and correctly described
- Verify any performance benchmarks or comparisons
- Flag anything that seems outdated or might have changed recently

BLOG CONTENT (for reference - read the file above for the canonical version):
{blog_content[:5000]}

Return a JSON object:
{{
  "verified_claims": ["List of claims verified as accurate"],
  "outdated_items": [
    {{
      "claim": "The specific outdated claim",
      "current_info": "What the current accurate information is",
      "source": "URL or reference for the correction"
    }}
  ],
  "unverifiable_claims": ["Claims that couldn't be verified"],
  "stale_data": [
    {{
      "data_point": "The stale data",
      "correction": "Updated data",
      "source": "Where the updated data comes from"
    }}
  ]
}}
"""
        return await self.run_prompt(
            prompt,
            workdir=repo_path,
            allowed_tools=["WebFetch", "WebSearch", "Read", "Glob", "Grep"],
            run_id=run_id,
            phase=phase,
        )

    async def post_pr_review_comment(
        self,
        github_token: str,
        repo: str,
        pr_number: int,
        primary_file_path: str,
        review_body: str,
        repo_path: str,
        run_id: int = None,
    ) -> dict:
        """
        Ask Claude Code to post a resolvable inline PR review comment using the GitHub API.
        Claude knows how to fetch the PR diff, find a valid line position, and post correctly.
        """
        # Escape the review body for embedding in the prompt
        escaped_body = review_body.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('`', '\\`')

        prompt = f"""You need to post a resolvable inline review comment on a GitHub Pull Request using the GitHub API via curl.

DETAILS:
- GitHub Token: {github_token}
- Repo: {repo}
- PR Number: {pr_number}
- Primary blog file in the PR: {primary_file_path}

STEPS:
1. First, fetch the PR details to get the latest commit SHA:
   curl -s -H "Authorization: token {github_token}" -H "Accept: application/vnd.github.v3+json" https://api.github.com/repos/{repo}/pulls/{pr_number}
   Extract the `head.sha` field.

2. Fetch the PR file diff to find a valid line position in the blog file:
   curl -s -H "Authorization: token {github_token}" -H "Accept: application/vnd.github.v3+json" "https://api.github.com/repos/{repo}/pulls/{pr_number}/files"
   Find the entry where `filename` matches or ends with "{primary_file_path.split('/')[-1] if '/' in primary_file_path else primary_file_path}".
   Parse the `patch` field to find a valid line number - look for `@@ -N,N +START,COUNT @@` and use START as the line number, or any line that appears as an added line (starts with +) in the diff. The line number must correspond to an actual line in the new version of the file that exists in the diff hunk.

3. Post the review with an inline resolvable comment using this API:
   POST https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews
   With body:
   {{
     "commit_id": "<sha from step 1>",
     "body": "Sentinel automated review - see inline comment for details.",
     "event": "REQUEST_CHANGES",
     "comments": [
       {{
         "path": "<relative file path from step 2>",
         "line": <valid line number from step 2>,
         "side": "RIGHT",
         "body": "<the review content>"
       }}
     ]
   }}

THE REVIEW CONTENT TO POST (use this exactly as the comment body):
---
{review_body}
---

Use Bash to run the curl commands. If the first line position fails with 422, try a different line number from the diff (e.g., line 2, 3, 5, or the last line of the first hunk).

Return "success" when the comment is posted successfully, or the error message if it fails.
"""
        return await self.run_prompt(
            prompt,
            workdir=repo_path,
            allowed_tools=["Bash"],
            run_id=run_id,
            phase="post_comment",
        )
