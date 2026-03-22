"""
Claude Code CLI Service - spawns Claude Code instances for AI-powered
blog review, fact-checking, and topic discovery.
"""
import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ClaudeService:
    def __init__(
        self,
        claude_cmd: str = "claude",
        setup_token: str = "",
        max_turns: int = 30,
        workdir: str = "",
    ):
        self.claude_cmd = claude_cmd
        self.setup_token = setup_token
        self.max_turns = max_turns
        self.workdir = workdir

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
        max_turns: int = 0,
        allowed_tools: list[str] = None,
    ) -> dict:
        """
        Run a prompt through Claude Code CLI and return the result.
        Uses --print flag for non-interactive output.
        """
        cwd = workdir or self.workdir
        turns = max_turns or self.max_turns

        cmd = [
            self.claude_cmd,
            "--print",
            "--max-turns", str(turns),
            "--output-format", "json",
        ]

        if allowed_tools:
            for tool in allowed_tools:
                cmd.extend(["--allowedTools", tool])

        logger.info(f"Running Claude Code in {cwd}: {prompt[:100]}...")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=self._build_env(),
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=prompt.encode()),
                timeout=600,  # 10 minute timeout
            )

            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")

            if process.returncode != 0:
                logger.error(f"Claude Code failed (exit {process.returncode}): {stderr_text[:500]}")
                return {
                    "success": False,
                    "error": stderr_text[:1000],
                    "exit_code": process.returncode,
                }

            # Try to parse JSON output
            try:
                result = json.loads(stdout_text)
                return {"success": True, "result": result}
            except json.JSONDecodeError:
                # Return raw text if not JSON
                return {"success": True, "result": stdout_text}

        except asyncio.TimeoutError:
            logger.error("Claude Code timed out after 600s")
            return {"success": False, "error": "Timeout after 600 seconds"}
        except Exception as e:
            logger.error(f"Claude Code error: {e}")
            return {"success": False, "error": str(e)}

    async def review_blog(self, blog_content: str, repo_path: str, existing_blogs_context: str = "") -> dict:
        """
        Use Claude Code to perform an editorial review of a blog post.
        Runs inside the cloned repo for full context.
        """
        prompt = f"""You are an expert blog editor and SEO specialist for a GPU cloud infrastructure company (Spheron).
Review the following blog post content and provide a detailed editorial review.

IMPORTANT RULES:
- Never use emdashes (--) anywhere in your review or suggestions. Use hyphens (-) instead.
- Score the blog from 1-10 on: Content Quality, SEO Optimization, Technical Accuracy, Readability, Internal Linking
- Check for proper cross-links to docs.spheron.ai where technical concepts are mentioned
- Check for proper cross-links to other existing blog posts on spheron.network/blog/
- Verify the blog follows the style and tone of existing blogs in this repository
- Check that all claims are factually accurate and up-to-date
- Look for any outdated data, stale references, or incorrect information

EXISTING BLOGS CONTEXT (for internal linking suggestions):
{existing_blogs_context[:3000]}

BLOG CONTENT TO REVIEW:
{blog_content}

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
            max_turns=15,
            allowed_tools=["Read", "Glob", "Grep", "WebFetch", "WebSearch"],
        )

    async def discover_topics(self, existing_topics: list[str], focus_areas: str, topic_count: int = 3) -> dict:
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
            max_turns=20,
            allowed_tools=["WebFetch", "WebSearch"],
        )

    async def fact_check_blog(self, blog_content: str, repo_path: str) -> dict:
        """
        Use Claude Code to fact-check a blog post with web search.
        """
        prompt = f"""You are a fact-checker for a technical blog about GPU cloud infrastructure.
Thoroughly fact-check the following blog content using web search to verify every claim.

IMPORTANT RULES:
- Never use emdashes (--) in any content. Use hyphens (-) instead.
- Search for each technical claim, statistic, benchmark, and comparison
- Verify model names, versions, and release dates are current
- Check that referenced tools, libraries, and frameworks are still active and correctly described
- Verify any performance benchmarks or comparisons
- Flag anything that seems outdated or might have changed recently

BLOG CONTENT:
{blog_content}

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
            max_turns=25,
            allowed_tools=["WebFetch", "WebSearch", "Read", "Glob", "Grep"],
        )
