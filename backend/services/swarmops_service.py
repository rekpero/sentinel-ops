"""
SwarmOps Integration Service - interfaces with the SwarmOps orchestrator
for plan creation, issue creation, and triggering blog writing agents.

API spec reference:
- Auth: Bearer token via API_KEYS env on SwarmOps side
- Planning: POST /api/planning -> poll GET /api/planning/{id} until generating==false
- Issue: POST /api/planning/{id}/create-issue -> returns issue_number + issue_url
- Workspaces: GET /api/workspaces -> { workspaces: [...] }
"""
import asyncio
import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Max time to wait for a plan to generate (seconds)
PLANNING_TIMEOUT = 600
PLANNING_POLL_INTERVAL = 5


class SwarmOpsService:
    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=60.0)

    async def close(self):
        await self.client.aclose()

    def _headers(self) -> dict:
        """Auth uses Bearer token per the SwarmOps API spec."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # === Workspaces ===

    async def list_workspaces(self) -> list[dict]:
        """
        GET /api/workspaces
        Response: { "workspaces": [ { "id", "name", "github_repo", "status", ... } ] }
        """
        resp = await self.client.get(
            f"{self.base_url}/api/workspaces",
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        # Response wraps workspaces in a "workspaces" key
        return data.get("workspaces", data if isinstance(data, list) else [])

    async def get_workspace_id(self, repo_name: str) -> Optional[str]:
        """
        Find workspace ID by matching github_repo field.
        repo_name should be "owner/repo" format.
        """
        workspaces = await self.list_workspaces()
        for ws in workspaces:
            github_repo = ws.get("github_repo", "")
            name = ws.get("name", "")
            # Match against github_repo field or name
            if repo_name in github_repo or repo_name.split("/")[-1] in name:
                ws_id = ws.get("id")
                logger.info(f"Found workspace '{name}' (id={ws_id}) for repo {repo_name}")
                return ws_id
        logger.warning(f"No workspace found matching repo: {repo_name}")
        return None

    # === Planning ===

    async def create_planning_session(self, workspace_id: str, message: str) -> dict:
        """
        POST /api/planning
        Body: { "workspace_id": "string", "message": "string" }
        Response: { "session": {...}, "messages": [...] }

        Planning starts immediately in background. Poll to wait for completion.
        """
        resp = await self.client.post(
            f"{self.base_url}/api/planning",
            json={"workspace_id": workspace_id, "message": message},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def get_planning_session(self, session_id: str) -> dict:
        """
        GET /api/planning/{session_id}
        Response: {
            "session": { "id", "workspace_id", "title", "status", "issue_number", ... },
            "messages": [ { "id", "role", "content", ... } ],
            "generating": bool
        }

        Key fields:
        - generating: true while planner is still running
        - session.status: "active" | "completed" | "error"
        - The plan is the last message with role=="assistant" (only valid when generating==false)
        """
        resp = await self.client.get(
            f"{self.base_url}/api/planning/{session_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def wait_for_plan(self, session_id: str, timeout: int = PLANNING_TIMEOUT) -> dict:
        """
        Poll a planning session until generating==false or timeout.
        Returns the full session response with the completed plan.
        Raises TimeoutError if planning takes too long.
        """
        elapsed = 0
        while elapsed < timeout:
            data = await self.get_planning_session(session_id)

            generating = data.get("generating", False)
            session_status = data.get("session", {}).get("status", "")

            if session_status == "error":
                logger.error(f"Planning session {session_id} errored")
                return data

            if not generating:
                logger.info(f"Planning session {session_id} completed")
                return data

            logger.debug(f"Planning session {session_id} still generating... ({elapsed}s)")
            await asyncio.sleep(PLANNING_POLL_INTERVAL)
            elapsed += PLANNING_POLL_INTERVAL

        raise TimeoutError(f"Planning session {session_id} timed out after {timeout}s")

    def extract_plan_from_session(self, session_data: dict) -> Optional[str]:
        """
        Extract the plan content from a completed session response.
        The plan is the last message with role=="assistant".
        """
        messages = session_data.get("messages", [])
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                return msg.get("content", "")
        return None

    async def get_planning_events(self, session_id: str, since: int = 0) -> list[dict]:
        """
        GET /api/planning/{session_id}/events?since={since}
        Returns incremental progress events from the planner.
        """
        resp = await self.client.get(
            f"{self.base_url}/api/planning/{session_id}/events",
            params={"since": since},
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("events", [])

    async def send_refinement(self, session_id: str, message: str) -> dict:
        """
        POST /api/planning/{session_id}/messages
        Body: { "message": "string" }

        Sends a follow-up to refine the plan. Re-triggers generation.
        After calling this, poll again until generating==false.
        Returns 409 if planning is already in progress.
        """
        resp = await self.client.post(
            f"{self.base_url}/api/planning/{session_id}/messages",
            json={"message": message},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def create_issue_from_plan(
        self,
        session_id: str,
        title: str = "",
        message_index: int = None,
    ) -> dict:
        """
        POST /api/planning/{session_id}/create-issue
        Body (optional): { "title": "string", "message_index": int|null }

        If title is empty, SwarmOps generates one via AI.
        If message_index is None, uses the last assistant message.

        Response: { "issue_number": 42, "issue_url": "https://...", "title": "..." }

        After success, session.status becomes "completed" and
        session.issue_number / session.issue_url are set.

        Errors:
        - 400: No plan found or invalid message_index
        - 409: Still generating or issue already created
        """
        body = {}
        if title:
            body["title"] = title
        if message_index is not None:
            body["message_index"] = message_index

        resp = await self.client.post(
            f"{self.base_url}/api/planning/{session_id}/create-issue",
            json=body if body else None,
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def cancel_planning(self, session_id: str) -> dict:
        """
        POST /api/planning/{session_id}/cancel
        Cancels an in-progress planning run. Safe to call even if not generating.
        """
        resp = await self.client.post(
            f"{self.base_url}/api/planning/{session_id}/cancel",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def delete_planning_session(self, session_id: str) -> dict:
        """
        DELETE /api/planning/{session_id}
        Deletes session, all messages, and all events. Cancels generation first.
        """
        resp = await self.client.delete(
            f"{self.base_url}/api/planning/{session_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    # === Agents & Metrics (dashboard endpoints) ===

    async def list_agents(self, status: str = "") -> list:
        """List agents with optional status filter."""
        params = {}
        if status:
            params["status"] = status
        resp = await self.client.get(
            f"{self.base_url}/api/agents",
            headers=self._headers(),
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_metrics(self) -> dict:
        """Get SwarmOps metrics."""
        resp = await self.client.get(
            f"{self.base_url}/api/metrics",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()
