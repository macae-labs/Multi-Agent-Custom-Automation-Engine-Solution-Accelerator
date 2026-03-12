"""Tests for PlannerAgent.

Unit tests (default, always run): mock Azure AI, validate logic.
Integration tests (@pytest.mark.integration): require real Azure services.
"""

import os
import sys
import unittest
import uuid
from typing import Any, List, Set
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from context.cosmos_memory_kernel import CosmosMemoryContext
from models.messages_kernel import (
    AgentType,
    InputTask,
    Plan,
    PlanStatus,
    Step,
    StepStatus,
)

load_dotenv()


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

class CleanupCosmosContext(CosmosMemoryContext):
    """Tracks created items for test cleanup."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.created_items: Set[str] = set()
        self.created_plans: Set[str] = set()
        self.created_steps: Set[str] = set()

    async def add_item(self, item: Any) -> None:
        await super().add_item(item)
        if hasattr(item, "id"):
            self.created_items.add(item.id)

    async def add_plan(self, plan: Plan) -> None:
        await super().add_plan(plan)
        self.created_plans.add(plan.id)

    async def add_step(self, step: Step) -> None:
        await super().add_step(step)
        self.created_steps.add(step.id)

    async def cleanup_test_data(self) -> None:
        for item_id in self.created_steps | self.created_plans | self.created_items:
            try:
                await self._delete_item_by_id(item_id)
            except Exception:
                pass

    async def _delete_item_by_id(self, item_id: str) -> None:
        if not self._container:
            await self.initialize()
        if not self._container:
            return
        try:
            params: List[dict] = [{"name": "@id", "value": item_id}]
            items = self._container.query_items(
                query="SELECT * FROM c WHERE c.id = @id",
                parameters=params,
                enable_cross_partition_query=True,
            )
            async for item in items:
                pk = item.get("session_id")
                if pk:
                    await self._container.delete_item(item=item_id, partition_key=pk)
                break
        except Exception:
            pass


def _make_mock_plan(session_id: str, user_id: str) -> Plan:
    return Plan(
        id=str(uuid.uuid4()),
        session_id=session_id,
        user_id=user_id,
        initial_goal="Create a marketing plan",
        overall_status=PlanStatus.in_progress,
        source=AgentType.PLANNER.value,
        summary="Marketing plan summary",
    )


def _make_mock_step(plan_id: str, session_id: str, user_id: str, agent=AgentType.MARKETING) -> Step:
    return Step(
        id=str(uuid.uuid4()),
        plan_id=plan_id,
        session_id=session_id,
        user_id=user_id,
        action="Create social media strategy",
        agent=agent,
        status=StepStatus.planned,
    )


# ---------------------------------------------------------------------------
# Unit tests — always run, no Azure
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPlannerAgentUnit(unittest.IsolatedAsyncioTestCase):
    """Validates PlannerAgent logic with mocked Azure AI."""

    def setUp(self):
        self.session_id = str(uuid.uuid4())
        self.user_id = "test-user"

    def _make_agent(self, memory_store):
        """Instantiate PlannerAgent without Azure SDK validation."""
        from kernel_agents.planner_agent import PlannerAgent

        agent = PlannerAgent.model_construct()
        agent._session_id = self.session_id
        agent._user_id = self.user_id
        agent._memory_store = memory_store
        agent._available_agents = [
            AgentType.HUMAN.value,
            AgentType.HR.value,
            AgentType.MARKETING.value,
            AgentType.PRODUCT.value,
            AgentType.PROCUREMENT.value,
            AgentType.TECH_SUPPORT.value,
            AgentType.GENERIC.value,
        ]
        agent._agent_tools_list = {}
        agent._tools = []
        agent._agent_instances = {}
        return agent

    async def test_handle_input_task_creates_plan_and_steps(self):
        """handle_input_task stores plan + steps in memory and returns success string."""
        mock_memory = MagicMock(spec=CosmosMemoryContext)
        mock_memory.add_item = AsyncMock()
        mock_memory.add_plan = AsyncMock()
        mock_memory.add_step = AsyncMock()
        mock_memory.get_plan_by_session = AsyncMock(return_value=None)

        agent = self._make_agent(mock_memory)

        plan = _make_mock_plan(self.session_id, self.user_id)
        steps = [
            _make_mock_step(plan.id, self.session_id, self.user_id),
            _make_mock_step(plan.id, self.session_id, self.user_id, AgentType.HR),
        ]

        with patch.object(agent, "_create_structured_plan", AsyncMock(return_value=(plan, steps))):
            result = await agent.handle_input_task(
                InputTask(session_id=self.session_id, description="Create a marketing plan")
            )

        self.assertIsInstance(result, str)
        self.assertIn("2", result)  # "Generated a plan with 2 steps"

    async def test_handle_input_task_redacts_pii(self):
        """PII in description is redacted before reaching _create_structured_plan."""
        mock_memory = MagicMock(spec=CosmosMemoryContext)
        mock_memory.add_item = AsyncMock()

        agent = self._make_agent(mock_memory)

        plan = _make_mock_plan(self.session_id, self.user_id)
        steps = [_make_mock_step(plan.id, self.session_id, self.user_id)]

        captured = {}

        async def capture_plan(input_task):
            captured["description"] = input_task.description
            return plan, steps

        with patch.object(agent, "_create_structured_plan", side_effect=capture_plan):
            await agent.handle_input_task(
                InputTask(
                    session_id=self.session_id,
                    description="Send email to john@example.com",
                )
            )

        self.assertNotIn("john@example.com", captured.get("description", ""))

    async def test_hr_agent_assigned_for_onboarding(self):
        """Steps for onboarding tasks include Hr_Agent."""
        mock_memory = MagicMock(spec=CosmosMemoryContext)
        mock_memory.add_item = AsyncMock()

        agent = self._make_agent(mock_memory)

        plan = _make_mock_plan(self.session_id, self.user_id)
        steps = [
            _make_mock_step(plan.id, self.session_id, self.user_id, AgentType.HR),
        ]

        with patch.object(agent, "_create_structured_plan", AsyncMock(return_value=(plan, steps))):
            await agent.handle_input_task(
                InputTask(session_id=self.session_id, description="Onboard Jessica Smith")
            )

        hr_steps = [s for s in steps if s.agent == AgentType.HR]
        self.assertGreater(len(hr_steps), 0)

    async def test_marketing_agent_not_assigned_for_onboarding(self):
        """Marketing agent should not appear in onboarding plans."""
        mock_memory = MagicMock(spec=CosmosMemoryContext)
        mock_memory.add_item = AsyncMock()

        agent = self._make_agent(mock_memory)

        plan = _make_mock_plan(self.session_id, self.user_id)
        steps = [_make_mock_step(plan.id, self.session_id, self.user_id, AgentType.HR)]

        with patch.object(agent, "_create_structured_plan", AsyncMock(return_value=(plan, steps))):
            await agent.handle_input_task(
                InputTask(session_id=self.session_id, description="Onboard Jessica Smith")
            )

        marketing_steps = [s for s in steps if s.agent == AgentType.MARKETING]
        self.assertEqual(len(marketing_steps), 0)

    async def test_plan_status_is_in_progress(self):
        """Created plan has in_progress status."""
        mock_memory = MagicMock(spec=CosmosMemoryContext)
        mock_memory.add_item = AsyncMock()

        agent = self._make_agent(mock_memory)

        plan = _make_mock_plan(self.session_id, self.user_id)
        steps = [_make_mock_step(plan.id, self.session_id, self.user_id)]

        with patch.object(agent, "_create_structured_plan", AsyncMock(return_value=(plan, steps))):
            await agent.handle_input_task(
                InputTask(session_id=self.session_id, description="Create a marketing plan")
            )

        self.assertEqual(plan.overall_status, PlanStatus.in_progress)

    async def test_cleanup_cosmos_context_tracks_plans(self):
        """CleanupCosmosContext tracks plan IDs for cleanup."""
        ctx = CleanupCosmosContext(session_id="s1", user_id="u1")
        plan = _make_mock_plan("s1", "u1")

        with patch.object(CosmosMemoryContext, "add_item", AsyncMock()):
            await ctx.add_plan(plan)

        self.assertIn(plan.id, ctx.created_plans)

    async def test_cleanup_cosmos_context_tracks_steps(self):
        """CleanupCosmosContext tracks step IDs for cleanup."""
        ctx = CleanupCosmosContext(session_id="s1", user_id="u1")
        plan = _make_mock_plan("s1", "u1")
        step = _make_mock_step(plan.id, "s1", "u1")

        with patch.object(CosmosMemoryContext, "add_item", AsyncMock()):
            await ctx.add_step(step)

        self.assertIn(step.id, ctx.created_steps)


# ---------------------------------------------------------------------------
# Integration tests — require real Azure AI + Cosmos DB
# ---------------------------------------------------------------------------

@pytest.mark.integration
class PlannerAgentIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """Integration tests against real Azure AI + Cosmos DB."""

    def setUp(self):
        required = [
            "AZURE_OPENAI_DEPLOYMENT_NAME",
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_AI_AGENT_ENDPOINT",
            "COSMOSDB_ENDPOINT",
            "COSMOSDB_DATABASE",
            "COSMOSDB_CONTAINER",
        ]
        missing = []
        invalid = []
        for var_name in required:
            value = (os.getenv(var_name) or "").strip()
            if not value:
                missing.append(var_name)
                continue
            if "mock" in value.lower():
                invalid.append(var_name)
        if missing:
            self.skipTest(f"Missing env vars: {missing}")
        if invalid:
            self.skipTest(f"Invalid mock values in env vars: {invalid}")
        self.session_id = str(uuid.uuid4())
        self.user_id = "test-user"
        self.memory_store = None
        self.planner_agent = None

    async def asyncTearDown(self):
        from kernel_agents.agent_factory import AgentFactory
        if self.memory_store:
            await self.memory_store.cleanup_test_data()
        AgentFactory.clear_cache(self.session_id)
        from app_config import config
        await config.close()

    async def _init(self):
        from app_config import config
        from kernel_agents.agent_factory import AgentFactory

        memory_store = CleanupCosmosContext(
            session_id=self.session_id,
            user_id=self.user_id,
        )
        await memory_store.initialize()
        self.memory_store = memory_store

        try:
            client = config.get_ai_project_client()
        except Exception as e:
            self.skipTest(f"Could not create AIProjectClient: {e}")

        agents = await AgentFactory.create_all_agents(
            session_id=self.session_id,
            user_id=self.user_id,
            memory_store=memory_store,
            client=client,
        )
        from kernel_agents.planner_agent import PlannerAgent
        planner = agents[AgentType.PLANNER]
        assert isinstance(planner, PlannerAgent)
        self.planner_agent = planner
        return self.planner_agent, memory_store

    async def test_handle_input_task(self):
        agent, memory = await self._init()
        await agent.handle_input_task(
            InputTask(session_id=self.session_id, description="Create a marketing plan for a product launch")
        )
        plan = await memory.get_plan_by_session(self.session_id)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.overall_status, PlanStatus.in_progress)
        steps = await memory.get_steps_for_plan(plan.id, self.session_id)
        self.assertGreater(len(steps), 0)

    async def test_hr_agent_selection(self):
        agent, memory = await self._init()
        await agent.handle_input_task(
            InputTask(session_id=self.session_id, description="Onboard a new employee, Jessica Smith.")
        )
        plan = await memory.get_plan_by_session(self.session_id)
        assert plan is not None
        steps = await memory.get_steps_for_plan(plan.id, self.session_id)
        hr_steps = [s for s in steps if s.agent == AgentType.HR]
        self.assertGreater(len(hr_steps), 0, "No steps assigned to Hr_Agent for onboarding")

    async def test_plan_generation_content(self):
        agent, memory = await self._init()
        await agent.handle_input_task(
            InputTask(session_id=self.session_id, description="Create a marketing plan for a product launch")
        )
        plan = await memory.get_plan_by_session(self.session_id)
        self.assertIsNotNone(plan)
        assert plan is not None
        steps = await memory.get_steps_for_plan(plan.id, self.session_id)
        marketing_terms = ["marketing", "product", "launch", "campaign", "strategy"]
        self.assertTrue(any(t in plan.initial_goal.lower() for t in marketing_terms))
        for step in steps:
            self.assertIsNotNone(step.action)
            self.assertEqual(step.status, StepStatus.planned)

    async def test_create_structured_plan(self):
        agent, memory = await self._init()
        plan, steps = await agent._create_structured_plan(
            InputTask(session_id=self.session_id, description="Arrange a technical webinar for our new SDK")
        )
        self.assertIsNotNone(plan)
        self.assertGreater(len(steps), 0)
