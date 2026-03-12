"""Tests for GroupChatManager.

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
    HumanFeedback,
    HumanFeedbackStatus,
    InputTask,
    Plan,
    PlanStatus,
    Step,
    StepStatus,
)

load_dotenv()


# ---------------------------------------------------------------------------
# Shared helpers
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


def _make_plan(session_id: str, user_id: str) -> Plan:
    return Plan(
        id=str(uuid.uuid4()),
        session_id=session_id,
        user_id=user_id,
        initial_goal="Create a marketing plan",
        overall_status=PlanStatus.in_progress,
        source=AgentType.PLANNER.value,
        summary="Marketing plan summary",
    )


def _make_step(plan_id: str, session_id: str, user_id: str,
               agent=AgentType.MARKETING, status=StepStatus.planned) -> Step:
    return Step(
        id=str(uuid.uuid4()),
        plan_id=plan_id,
        session_id=session_id,
        user_id=user_id,
        action="Create social media strategy",
        agent=agent,
        status=status,
    )


def _make_gcm(session_id: str, user_id: str, memory_store, agent_instances=None):
    """Instantiate GroupChatManager without Azure SDK validation."""
    from kernel_agents.group_chat_manager import GroupChatManager

    gcm = GroupChatManager.model_construct()
    gcm._session_id = session_id
    gcm._user_id = user_id
    gcm._memory_store = memory_store
    gcm._agent_instances = agent_instances or {}
    gcm._available_agents = [
        AgentType.HUMAN.value,
        AgentType.HR.value,
        AgentType.MARKETING.value,
        AgentType.PRODUCT.value,
        AgentType.PROCUREMENT.value,
        AgentType.TECH_SUPPORT.value,
        AgentType.GENERIC.value,
    ]
    gcm._agent_tools_list = []
    return gcm


# ---------------------------------------------------------------------------
# Unit tests — always run, no Azure
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGroupChatManagerUnit(unittest.IsolatedAsyncioTestCase):
    """Validates GroupChatManager logic with mocked dependencies."""

    def setUp(self):
        self.session_id = str(uuid.uuid4())
        self.user_id = "test-user"

    async def test_handle_input_task_delegates_to_planner(self):
        """handle_input_task calls planner_agent.handle_input_task."""
        mock_memory = MagicMock(spec=CosmosMemoryContext)
        mock_memory.add_item = AsyncMock()
        mock_memory.get_plan_by_session = AsyncMock(return_value=None)

        plan = _make_plan(self.session_id, self.user_id)

        mock_planner = MagicMock()
        mock_planner.handle_input_task = AsyncMock(return_value=plan)

        gcm = _make_gcm(
            self.session_id, self.user_id, mock_memory,
            agent_instances={AgentType.PLANNER.value: mock_planner},
        )

        input_task = InputTask(session_id=self.session_id, description="Create a marketing plan")
        result = await gcm.handle_input_task(input_task)

        mock_planner.handle_input_task.assert_called_once_with(input_task)
        self.assertEqual(result, plan)

    async def test_handle_human_feedback_approved_updates_step(self):
        """Approved feedback marks step as accepted and triggers execution."""
        plan = _make_plan(self.session_id, self.user_id)
        step = _make_step(plan.id, self.session_id, self.user_id)

        mock_memory = MagicMock(spec=CosmosMemoryContext)
        mock_memory.add_item = AsyncMock()
        mock_memory.get_steps_by_plan = AsyncMock(return_value=[step])
        mock_memory.get_plan_by_session = AsyncMock(return_value=plan)
        mock_memory.update_step = AsyncMock()
        mock_memory.update_plan = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.handle_action_request = AsyncMock(return_value="done")

        gcm = _make_gcm(
            self.session_id, self.user_id, mock_memory,
            agent_instances={
                AgentType.PLANNER.value: MagicMock(),
                AgentType.MARKETING.value: mock_agent,
            },
        )

        feedback = HumanFeedback(
            session_id=self.session_id,
            plan_id=plan.id,
            step_id=step.id,
            approved=True,
            human_feedback="Looks good",
        )

        with patch.object(gcm, "_execute_step", AsyncMock(return_value=None)):
            await gcm.handle_human_feedback(feedback)

        mock_memory.get_steps_by_plan.assert_called_once_with(plan.id)

    async def test_handle_human_feedback_rejected_does_not_execute(self):
        """Rejected feedback does not trigger step execution."""
        plan = _make_plan(self.session_id, self.user_id)
        step = _make_step(plan.id, self.session_id, self.user_id)

        mock_memory = MagicMock(spec=CosmosMemoryContext)
        mock_memory.add_item = AsyncMock()
        mock_memory.get_steps_by_plan = AsyncMock(return_value=[step])
        mock_memory.get_plan_by_session = AsyncMock(return_value=plan)
        mock_memory.update_step = AsyncMock()
        mock_memory.update_plan = AsyncMock()

        gcm = _make_gcm(self.session_id, self.user_id, mock_memory)

        feedback = HumanFeedback(
            session_id=self.session_id,
            plan_id=plan.id,
            step_id=step.id,
            approved=False,
        )

        execute_mock = AsyncMock()
        with patch.object(gcm, "_execute_step", execute_mock):
            await gcm.handle_human_feedback(feedback)

        execute_mock.assert_not_called()

    async def test_conversation_history_includes_plan_summary(self):
        """_execute_step action payload includes plan summary in conversation history."""
        plan = _make_plan(self.session_id, self.user_id)
        steps = [_make_step(plan.id, self.session_id, self.user_id)]

        mock_memory = MagicMock(spec=CosmosMemoryContext)
        mock_memory.update_step = AsyncMock()
        mock_memory.add_item = AsyncMock()
        mock_memory.get_plan_by_session = AsyncMock(return_value=plan)
        mock_memory.get_steps_by_plan = AsyncMock(return_value=steps)

        mock_agent = MagicMock()
        mock_agent.handle_action_request = AsyncMock()

        gcm = _make_gcm(
            self.session_id,
            self.user_id,
            mock_memory,
            agent_instances={AgentType.MARKETING.value: mock_agent},
        )

        await gcm._execute_step(self.session_id, steps[0])

        mock_agent.handle_action_request.assert_called_once()
        sent_request = mock_agent.handle_action_request.call_args.args[0]
        self.assertIn(plan.summary, sent_request.action)

    async def test_cleanup_cosmos_context_tracks_plans(self):
        ctx = CleanupCosmosContext(session_id="s1", user_id="u1")
        plan = _make_plan("s1", "u1")
        with patch.object(CosmosMemoryContext, "add_item", AsyncMock()):
            await ctx.add_plan(plan)
        self.assertIn(plan.id, ctx.created_plans)

    async def test_cleanup_cosmos_context_tracks_steps(self):
        ctx = CleanupCosmosContext(session_id="s1", user_id="u1")
        plan = _make_plan("s1", "u1")
        step = _make_step(plan.id, "s1", "u1")
        with patch.object(CosmosMemoryContext, "add_item", AsyncMock()):
            await ctx.add_step(step)
        self.assertIn(step.id, ctx.created_steps)


# ---------------------------------------------------------------------------
# Integration tests — require real Azure AI + Cosmos DB
# ---------------------------------------------------------------------------

@pytest.mark.integration
class GroupChatManagerIntegrationTest(unittest.IsolatedAsyncioTestCase):
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
        self.gcm = None

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
        from kernel_agents.group_chat_manager import GroupChatManager
        gcm_agent = agents[AgentType.GROUP_CHAT_MANAGER]
        assert isinstance(gcm_agent, GroupChatManager)
        self.gcm: GroupChatManager = gcm_agent
        return self.gcm, memory_store

    async def test_handle_input_task(self):
        gcm, memory = await self._init()
        input_task = InputTask(
            session_id=self.session_id,
            description="Create a marketing plan for a product launch",
        )
        await gcm.handle_input_task(input_task)
        plan = await memory.get_plan_by_session(self.session_id)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.overall_status, PlanStatus.in_progress)
        steps = await memory.get_steps_for_plan(plan.id, self.session_id)
        self.assertGreater(len(steps), 0)

    async def test_human_feedback_approved(self):
        gcm, memory = await self._init()
        input_task = InputTask(
            session_id=self.session_id,
            description="Create a marketing plan for a product launch",
        )
        await gcm.handle_input_task(input_task)
        plan = await memory.get_plan_by_session(self.session_id)
        self.assertIsNotNone(plan)
        assert plan is not None
        steps = await memory.get_steps_for_plan(plan.id, self.session_id)
        self.assertGreater(len(steps), 0)
        first_step = steps[0]
        feedback = HumanFeedback(
            session_id=self.session_id,
            plan_id=plan.id,
            step_id=first_step.id,
            approved=True,
            human_feedback="Looks good, proceed.",
        )
        await self.gcm.handle_human_feedback(feedback)
        assert self.memory_store is not None
        updated = await self.memory_store.get_step(first_step.id, self.session_id)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.human_approval_status, HumanFeedbackStatus.accepted)

    async def test_execute_next_step(self):
        gcm, memory = await self._init()
        input_task = InputTask(
            session_id=self.session_id,
            description="Create a marketing plan for a product launch",
        )
        await gcm.handle_input_task(input_task)
        plan = await memory.get_plan_by_session(self.session_id)
        self.assertIsNotNone(plan)
        assert plan is not None
        steps = await memory.get_steps_for_plan(plan.id, self.session_id)
        self.assertGreater(len(steps), 0)
        feedback = HumanFeedback(
            session_id=self.session_id,
            plan_id=plan.id,
            step_id=steps[0].id,
            approved=True,
        )
        await gcm.handle_human_feedback(feedback)
        assert self.memory_store is not None
        updated_steps = await self.memory_store.get_steps_for_plan(plan.id, self.session_id)
        non_planned = [s for s in updated_steps if s.status != StepStatus.planned]
        self.assertGreaterEqual(len(non_planned), 1)

    async def test_conversation_history_generation(self):
        gcm, memory = await self._init()
        input_task = InputTask(
            session_id=self.session_id,
            description="Create a marketing plan for a product launch",
        )
        await gcm.handle_input_task(input_task)
        plan = await memory.get_plan_by_session(self.session_id)
        self.assertIsNotNone(plan)
        assert plan is not None
        steps = await memory.get_steps_for_plan(plan.id, self.session_id)
        self.assertGreater(len(steps), 0)

        target_step = next((s for s in steps if s.agent != AgentType.HUMAN), steps[0])
        target_agent = gcm._agent_instances[target_step.agent.value]
        original_handle = target_agent.handle_action_request
        mock_handle = AsyncMock(return_value=None)
        target_agent.handle_action_request = mock_handle
        try:
            await gcm._execute_step(self.session_id, target_step)
        finally:
            target_agent.handle_action_request = original_handle

        mock_handle.assert_called_once()
        action_request = mock_handle.call_args.args[0]
        self.assertIn("<conversation_history>", action_request.action)
        self.assertIn(plan.summary, action_request.action)
