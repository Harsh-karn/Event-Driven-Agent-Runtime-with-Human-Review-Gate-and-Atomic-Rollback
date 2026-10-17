"""Concrete Demo Digest Agent for Tether execution layer.

Autonomous engineering digest and release triage agent demonstrating:
1. Immediate read tool execution (fetching commits and telemetry).
2. Autonomous LLM synthesis via Runtime interface.
3. Quarantined side-effect staging (Slack broadcast & release tagging).
4. Paired compensation actions for atomic rollback.
"""

import logging
from typing import Any, Optional
from uuid import UUID

from tether.agents.base import AgentContext
from tether.effects import ToolRegistry

logger = logging.getLogger(__name__)


class DemoDigestAgent:
    """Tether demo agent that digests commits/metrics and stages operational broadcasts."""

    agent_id: str = "demo_digest"
    model: str = "gpt-4o-mini"

    def __init__(self, model: Optional[str] = None):
        if model:
            self.model = model
        self.tools = ToolRegistry()
        self._register_tools()

    def _register_tools(self) -> None:
        """Register read and effect tools with registered compensations."""

        # ----------------------------------------------------------------------
        # READ TOOL: fetch_git_commits (executes immediately)
        # ----------------------------------------------------------------------
        async def handle_fetch_commits(
            repo: str = "tether/core", since_hours: int = 24
        ) -> dict[str, Any]:
            logger.info(f"Executing read tool 'fetch_git_commits' for repo '{repo}'")
            return {
                "repo": repo,
                "commits_analyzed": 3,
                "commits": [
                    {
                        "sha": "aefe707",
                        "author": "dev-lead",
                        "message": "feat(core): implement deterministic finite state machine with strict transitions",
                    },
                    {
                        "sha": "687bd91",
                        "author": "sec-eng",
                        "message": "feat(effects): introduce cryptographic SHA-256 effect hashing and quarantine",
                    },
                    {
                        "sha": "b35e53a",
                        "author": "ops-lead",
                        "message": "feat(rollback): implement atomic reverse-compensation unwind engine",
                    },
                ],
            }

        self.tools.register_read_tool(
            name="fetch_git_commits",
            description="Retrieve recent git commits and diffs for an engineering repository.",
            parameters={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (e.g. 'tether/core')",
                    },
                    "since_hours": {
                        "type": "integer",
                        "description": "Hours of commit history to inspect",
                        "default": 24,
                    },
                },
                "required": ["repo"],
            },
            handler=handle_fetch_commits,
            target_system="git_provider",
        )

        # ----------------------------------------------------------------------
        # READ TOOL: fetch_system_metrics (executes immediately)
        # ----------------------------------------------------------------------
        async def handle_fetch_metrics(
            environment: str = "production",
        ) -> dict[str, Any]:
            logger.info(f"Executing read tool 'fetch_system_metrics' for env '{environment}'")
            return {
                "environment": environment,
                "uptime_percent": 99.98,
                "active_workers": 4,
                "pgmq_queue_depth": 0,
                "avg_step_latency_ms": 42.5,
                "incident_count": 0,
            }

        self.tools.register_read_tool(
            name="fetch_system_metrics",
            description="Retrieve runtime cluster health, uptime, and queue performance telemetry.",
            parameters={
                "type": "object",
                "properties": {
                    "environment": {
                        "type": "string",
                        "description": "Deployment environment name",
                        "default": "production",
                    }
                },
            },
            handler=handle_fetch_metrics,
            target_system="cloud_monitoring",
        )

        # ----------------------------------------------------------------------
        # EFFECT TOOL: post_slack_broadcast (NEVER executes during step phase)
        # ----------------------------------------------------------------------
        def generate_slack_compensation(params: dict[str, Any]) -> dict[str, Any]:
            channel = params.get("channel", "#engineering-leads")
            return {
                "channel": channel,
                "lookup_key": f"broadcast_{channel.replace('#', '')}",
                "reason": "Atomic rollback triggered due to subsequent commit step failure",
            }

        async def execute_slack_broadcast(params: dict[str, Any]) -> dict[str, Any]:
            logger.info(f"COMMITTING: Posting Slack broadcast to {params.get('channel')}")
            return {"status": "posted", "channel": params.get("channel"), "message_id": "msg_99482"}

        async def compensate_slack_broadcast(params: dict[str, Any]) -> dict[str, Any]:
            logger.info(f"COMPENSATING: Removing Slack broadcast from {params.get('channel')}")
            return {"status": "deleted", "channel": params.get("channel"), "deleted": True}

        self.tools.register_effect_tool(
            name="post_slack_broadcast",
            description="Stage a public engineering summary announcement to a team Slack channel.",
            parameters={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Slack channel (e.g. '#engineering-leads')",
                    },
                    "headline": {
                        "type": "string",
                        "description": "High-level headline for digest",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Detailed bulleted summary of key achievements",
                    },
                },
                "required": ["channel", "headline", "summary"],
            },
            compensation_action="delete_slack_broadcast",
            compensation_generator=generate_slack_compensation,
            target_system="slack_webhook",
            execution_handler=execute_slack_broadcast,
            compensation_handler=compensate_slack_broadcast,
        )

        # ----------------------------------------------------------------------
        # EFFECT TOOL: tag_git_release (NEVER executes during step phase)
        # ----------------------------------------------------------------------
        def generate_git_compensation(params: dict[str, Any]) -> dict[str, Any]:
            return {
                "repo": params.get("repo", "tether/core"),
                "tag": params.get("tag", "v0.1.0"),
            }

        async def execute_tag_release(params: dict[str, Any]) -> dict[str, Any]:
            logger.info(f"COMMITTING: Tagging release {params.get('tag')} on {params.get('repo')}")
            return {"status": "tagged", "tag": params.get("tag"), "sha": params.get("commit_sha")}

        async def compensate_tag_release(params: dict[str, Any]) -> dict[str, Any]:
            logger.info(f"COMPENSATING: Deleting release tag {params.get('tag')} on {params.get('repo')}")
            return {"status": "deleted_tag", "tag": params.get("tag")}

        self.tools.register_effect_tool(
            name="tag_git_release",
            description="Stage an immutable release version tag in the git repository.",
            parameters={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository name",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Semantic version tag (e.g. 'v0.1.0')",
                    },
                    "commit_sha": {
                        "type": "string",
                        "description": "Target commit hash",
                    },
                },
                "required": ["repo", "tag", "commit_sha"],
            },
            compensation_action="delete_git_release_tag",
            compensation_generator=generate_git_compensation,
            target_system="github_api",
            execution_handler=execute_tag_release,
            compensation_handler=compensate_tag_release,
        )

    def get_system_prompt(self, context: AgentContext) -> str:
        """Produce deterministic system prompt for the digest agent."""
        return (
            "You are Tether's Autonomous Engineering Digest & Triage Agent.\n"
            "Your objective is to inspect recent repo activity and metrics, synthesize an "
            "executive technical summary, and stage operational notifications.\n\n"
            "OPERATING RULES:\n"
            "1. First, call read tools ('fetch_git_commits' and 'fetch_system_metrics') to gather factual context.\n"
            "2. Second, stage an operational notification using 'post_slack_broadcast' and a version tag via 'tag_git_release'.\n"
            "3. Remember that side-effect tools will be QUARANTINED in Postgres and will NEVER execute immediately.\n"
            "4. A human reviewer will review your staged actions before any commit occurs.\n"
            "5. After staging necessary actions, provide a concise final briefing."
        )

    def build_initial_messages(self, context: AgentContext) -> list[dict[str, Any]]:
        """Construct user trigger prompt."""
        repo = context.payload.get("repo", "tether/core")
        period = context.payload.get("period", "daily")
        channel = context.payload.get("channel", "#engineering-leads")

        return [
            {
                "role": "user",
                "content": (
                    f"Trigger Event received via '{context.trigger}':\n"
                    f"Please generate the {period} engineering digest for '{repo}'. "
                    f"Inspect the commits and health telemetry, then stage a broadcast to '{channel}'."
                ),
            }
        ]
