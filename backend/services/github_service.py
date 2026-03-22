"""
GitHub service - handles all GitHub API interactions.
Supports: issues, PRs, line comments, review comments, labels.
"""
import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubService:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo  # "owner/repo"
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        self.client = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers=self.headers,
            timeout=30.0,
        )

    async def close(self):
        await self.client.aclose()

    # === Issues ===

    async def list_issues(self, labels: str = "", state: str = "open", per_page: int = 50):
        """List issues with optional label filter."""
        params = {"state": state, "per_page": per_page}
        if labels:
            params["labels"] = labels
        resp = await self.client.get(f"/repos/{self.repo}/issues", params=params)
        resp.raise_for_status()
        return resp.json()

    async def create_issue(self, title: str, body: str, labels: list[str] = None):
        """Create a new issue."""
        data = {"title": title, "body": body}
        if labels:
            data["labels"] = labels
        resp = await self.client.post(f"/repos/{self.repo}/issues", json=data)
        resp.raise_for_status()
        return resp.json()

    async def add_issue_comment(self, issue_number: int, body: str):
        """Add a general comment to an issue."""
        resp = await self.client.post(
            f"/repos/{self.repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        resp.raise_for_status()
        return resp.json()

    # === Pull Requests ===

    async def list_prs(self, state: str = "open", labels: str = "", per_page: int = 50):
        """List pull requests. Note: GitHub Issues API includes PRs when filtering by label."""
        params = {"state": state, "per_page": per_page}
        if labels:
            params["labels"] = labels
        # Use issues endpoint for label filtering (PRs have pull_request key)
        resp = await self.client.get(f"/repos/{self.repo}/issues", params=params)
        resp.raise_for_status()
        items = resp.json()
        return [i for i in items if "pull_request" in i]

    async def get_pr(self, pr_number: int):
        """Get a single PR."""
        resp = await self.client.get(f"/repos/{self.repo}/pulls/{pr_number}")
        resp.raise_for_status()
        return resp.json()

    async def get_pr_files(self, pr_number: int):
        """Get files changed in a PR."""
        resp = await self.client.get(
            f"/repos/{self.repo}/pulls/{pr_number}/files",
            params={"per_page": 100},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_pr_diff(self, pr_number: int):
        """Get the raw diff of a PR."""
        headers = {**self.headers, "Accept": "application/vnd.github.v3.diff"}
        resp = await self.client.get(
            f"/repos/{self.repo}/pulls/{pr_number}",
            headers=headers,
        )
        resp.raise_for_status()
        return resp.text

    # === PR Line Comments (Resolvable Review Comments) ===

    async def create_pr_review_comment(
        self,
        pr_number: int,
        body: str,
        commit_id: str,
        path: str,
        line: int,
        side: str = "RIGHT",
    ):
        """
        Create a line-specific review comment on a PR.
        These are resolvable comments tied to a specific line in the diff.
        This is what SwarmOps picks up and acts on.
        """
        data = {
            "body": body,
            "commit_id": commit_id,
            "path": path,
            "line": line,
            "side": side,
        }
        resp = await self.client.post(
            f"/repos/{self.repo}/pulls/{pr_number}/comments",
            json=data,
        )
        resp.raise_for_status()
        return resp.json()

    async def create_pr_review_with_comments(
        self,
        pr_number: int,
        body: str,
        event: str = "COMMENT",
        comments: list[dict] = None,
    ):
        """
        Create a PR review with multiple line comments at once.
        event: COMMENT, APPROVE, REQUEST_CHANGES
        comments: list of {path, line, side, body}
        """
        data = {"body": body, "event": event}
        if comments:
            data["comments"] = comments
        resp = await self.client.post(
            f"/repos/{self.repo}/pulls/{pr_number}/reviews",
            json=data,
        )
        resp.raise_for_status()
        return resp.json()

    # === General PR Comments (Non-resolvable, for tagging humans) ===

    async def add_pr_comment(self, pr_number: int, body: str):
        """
        Add a general (non-line) comment to a PR.
        Used for tagging humans - these cannot be resolved.
        """
        return await self.add_issue_comment(pr_number, body)

    # === Review Comment Management ===

    async def list_review_comments(self, pr_number: int):
        """List all review comments on a PR."""
        resp = await self.client.get(
            f"/repos/{self.repo}/pulls/{pr_number}/comments",
            params={"per_page": 100},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_review_comment_replies(self, comment_id: int):
        """Get replies to a specific review comment."""
        resp = await self.client.get(
            f"/repos/{self.repo}/pulls/comments/{comment_id}",
        )
        resp.raise_for_status()
        return resp.json()

    # === GraphQL for unresolved threads ===

    async def get_unresolved_review_threads(self, pr_number: int):
        """Use GraphQL to get unresolved review threads."""
        owner, repo = self.repo.split("/")
        query = """
        query($owner: String!, $repo: String!, $pr: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $pr) {
              reviewThreads(first: 100) {
                nodes {
                  isResolved
                  comments(first: 20) {
                    nodes {
                      body
                      author { login }
                      path
                      line
                      createdAt
                    }
                  }
                }
              }
            }
          }
        }
        """
        resp = await self.client.post(
            "https://api.github.com/graphql",
            json={
                "query": query,
                "variables": {"owner": owner, "repo": repo, "pr": pr_number},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        threads = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        return [t for t in threads if not t.get("isResolved")]

    # === Commits ===

    async def get_pr_commits(self, pr_number: int):
        """Get all commits in a PR."""
        resp = await self.client.get(
            f"/repos/{self.repo}/pulls/{pr_number}/commits",
            params={"per_page": 100},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_latest_commit_sha(self, pr_number: int) -> str:
        """Get the latest commit SHA of a PR."""
        commits = await self.get_pr_commits(pr_number)
        if commits:
            return commits[-1]["sha"]
        return ""

    # === Labels ===

    async def add_labels(self, issue_number: int, labels: list[str]):
        """Add labels to an issue or PR."""
        resp = await self.client.post(
            f"/repos/{self.repo}/issues/{issue_number}/labels",
            json={"labels": labels},
        )
        resp.raise_for_status()
        return resp.json()

    # === Repository ===

    async def get_repo_contents(self, path: str = "", ref: str = ""):
        """Get contents of a path in the repo."""
        params = {}
        if ref:
            params["ref"] = ref
        resp = await self.client.get(
            f"/repos/{self.repo}/contents/{path}",
            params=params,
        )
        resp.raise_for_status()
        return resp.json()
