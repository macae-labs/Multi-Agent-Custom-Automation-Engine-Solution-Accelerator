"""Unit tests for human_approval_manager module.

Comprehensive test cases covering HumanApprovalMagenticManager with proper mocking.
"""

import unittest
from unittest.mock import AsyncMock, Mock, patch

# Mock external Azure dependencies


# Mock agent_framework dependencies
class MockChatMessage:
    """Mock ChatMessage class."""

    def __init__(self, text="Mock message"):
        self.text = text
        self.role = "assistant"


class MockMagenticContext:
    """Mock MagenticContext class."""

    def __init__(self, task=None, round_count=0):
        self.task = task or MockChatMessage("Test task")
        self.round_count = round_count
        self.participant_descriptions = {
            "TestAgent1": "A test agent",
            "TestAgent2": "Another test agent",
        }
        # Source's _get_uncalled_agents() scans chat_history author_names
        self.chat_history = []


class MockStandardMagenticManager:
    """Mock StandardMagenticManager class."""

    def __init__(self, *args, **kwargs):
        self.task_ledger = None
        self.kwargs = kwargs

    async def plan(self, magentic_context):
        """Mock plan method."""
        self.task_ledger = Mock()
        self.task_ledger.plan = Mock()
        self.task_ledger.plan.text = "Test plan text"
        self.task_ledger.facts = Mock()
        self.task_ledger.facts.text = "Test facts"
        return MockChatMessage("Test plan")

    async def replan(self, magentic_context):
        """Mock replan method."""
        return MockChatMessage("Test replan")

    async def create_progress_ledger(self, magentic_context):
        """Mock create_progress_ledger method."""
        ledger = Mock()
        ledger.is_request_satisfied = Mock()
        ledger.is_request_satisfied.answer = False
        ledger.is_request_satisfied.reason = "In progress"
        ledger.is_in_loop = Mock()
        ledger.is_in_loop.answer = True
        ledger.is_in_loop.reason = "Continuing"
        ledger.is_progress_being_made = Mock()
        ledger.is_progress_being_made.answer = True
        ledger.is_progress_being_made.reason = "Making progress"
        ledger.next_speaker = Mock()
        ledger.next_speaker.answer = "TestAgent1"
        ledger.next_speaker.reason = "Agent turn"
        ledger.instruction_or_question = Mock()
        ledger.instruction_or_question.answer = "Continue with task"
        ledger.instruction_or_question.reason = "Next step"
        return ledger

    async def prepare_final_answer(self, magentic_context):
        """Mock prepare_final_answer method."""
        return MockChatMessage("Final answer")


# Mock constants from agent_framework
ORCHESTRATOR_FINAL_ANSWER_PROMPT = "Final answer prompt"
ORCHESTRATOR_TASK_LEDGER_PLAN_PROMPT = "Task ledger plan prompt"
ORCHESTRATOR_TASK_LEDGER_PLAN_UPDATE_PROMPT = "Task ledger plan update prompt"
ORCHESTRATOR_PROGRESS_LEDGER_PROMPT = "Progress ledger prompt"


# Mock v4.models.messages


# Mock v4.config.settings
mock_connection_config = Mock()
mock_connection_config.send_status_update_async = AsyncMock()

mock_orchestration_config = Mock()
mock_orchestration_config.max_rounds = 10
mock_orchestration_config.default_timeout = 30
mock_orchestration_config.managers = {}


# Mock v4.models.models
class MockMPlan:
    """Mock MPlan."""

    def __init__(self):
        self.id = "test-plan-id"
        self.user_id = None


# Mock v4.orchestration.helper.plan_to_mplan_converter


# Now import the module under test
import pytest
from agent_framework_orchestrations._magentic import (
    StandardMagenticManager,  # noqa: E402
)

from v4.models.models import MPlan
from v4.orchestration.human_approval_manager import HumanApprovalMagenticManager

connection_config = mock_connection_config
orchestration_config = mock_orchestration_config


@pytest.fixture(autouse=True)
def _collaborators_patched(monkeypatch):
    """Colaboradores de v4.orchestration.human_approval_manager parcheados en SU namespace y sólo durante cada
    test. Antes eran Mocks instalados en sys.modules a nivel de módulo para
    todo el proceso (INC-2026-004)."""
    import importlib

    mod = importlib.import_module("v4.orchestration.human_approval_manager")
    for name, value in (
        ("connection_config", connection_config),
        ("orchestration_config", orchestration_config),
    ):
        monkeypatch.setattr(mod, name, value)


@pytest.fixture(autouse=True)
def _base_manager_patched(monkeypatch):
    """Los métodos del StandardMagenticManager que invocan al modelo (plan,
    replan, create_progress_ledger, prepare_final_answer) se sustituyen por el
    doble ya existente MockStandardMagenticManager, en la clase base y sólo
    durante cada test. Lo que se prueba es la extensión HITL del producto."""
    base = MockStandardMagenticManager()

    async def _plan(self, magentic_context):
        message = await base.plan(magentic_context)
        self.task_ledger = base.task_ledger
        return message

    async def _replan(self, magentic_context=None, **_kwargs):
        return await base.replan(magentic_context)

    async def _create_progress_ledger(self, magentic_context):
        return await base.create_progress_ledger(magentic_context)

    async def _prepare_final_answer(self, magentic_context):
        return await base.prepare_final_answer(magentic_context)

    for name, value in (
        ("plan", _plan),
        ("replan", _replan),
        ("create_progress_ledger", _create_progress_ledger),
        ("prepare_final_answer", _prepare_final_answer),
    ):
        monkeypatch.setattr(StandardMagenticManager, name, value)


class TestHumanApprovalMagenticManager(unittest.IsolatedAsyncioTestCase):
    """Test cases for HumanApprovalMagenticManager class."""

    def setUp(self):
        """Set up test fixtures before each test method."""
        # Reset mocks
        connection_config.send_status_update_async.reset_mock()
        connection_config.send_status_update_async.side_effect = (
            None  # Reset side effects
        )

        # Create mock agent for new API
        self.mock_agent = Mock()
        self.mock_agent.name = "MockAgent"

        # Create test instance
        self.user_id = "test_user_123"
        self.manager = HumanApprovalMagenticManager(
            user_id=self.user_id,
            agent=self.mock_agent,
        )
        self.test_context = MockMagenticContext()

    def test_init(self):
        """Test HumanApprovalMagenticManager initialization."""
        # Test basic initialization
        mock_agent = Mock()
        manager = HumanApprovalMagenticManager(
            user_id="test_user",
            agent=mock_agent,
        )

        self.assertEqual(manager.current_user_id, "test_user")
        self.assertTrue(manager.approval_enabled)
        self.assertIsNone(manager.magentic_plan)

    def test_init_with_additional_kwargs(self):
        """Los kwargs llegan al StandardMagenticManager real (su contrato)."""
        additional_kwargs = {
            "max_round_count": 5,
            "max_stall_count": 4,
            "max_reset_count": 1,
        }

        mock_agent = Mock()
        manager = HumanApprovalMagenticManager(
            user_id="test_user",
            agent=mock_agent,
            **additional_kwargs,
        )

        self.assertEqual(manager.current_user_id, "test_user")
        # Verify kwargs were passed through
        self.assertEqual(manager.max_round_count, 5)
        self.assertEqual(manager.max_stall_count, 4)
        self.assertEqual(manager.max_reset_count, 1)

    async def test_plan_builds_mplan_and_never_waits(self):
        """plan() returns the base message and builds the MPlan. It sends nothing,
        waits for nothing and registers nothing in-process: the approval gate is
        the framework's request_info(MagenticPlanReviewRequest) parked in the
        checkpoint (OrchestrationManager._park_on_request_info)."""
        result = await self.manager.plan(self.test_context)

        self.assertIsInstance(result, MockChatMessage)
        self.assertEqual(result.text, "Test plan")
        self.assertIsNotNone(self.manager.magentic_plan)
        self.assertEqual(self.manager.magentic_plan.user_id, self.user_id)
        connection_config.send_status_update_async.assert_not_called()
        self.assertFalse(hasattr(self.manager, "_wait_for_user_approval"))

    async def test_replan_refreshes_mplan(self):
        """A native `revise` triggers manager.replan(); the MPlan the UI will see
        on the next PLAN_APPROVAL_REQUEST must be the replanned one."""
        await self.manager.plan(self.test_context)
        first = self.manager.magentic_plan
        self.manager.task_ledger.plan.text = "- **MockAgent** to do it differently"
        result = await self.manager.replan(self.test_context)

        self.assertEqual(result.text, "Test replan")
        self.assertIsNot(self.manager.magentic_plan, first)
        connection_config.send_status_update_async.assert_not_called()

    async def test_plan_task_ledger_none(self):
        """Test plan method when task_ledger is None."""
        # Setup - simulate task_ledger being None after super().plan()
        with patch.object(self.manager, "plan", wraps=self.manager.plan):
            with patch(
                "v4.orchestration.human_approval_manager.StandardMagenticManager.plan"
            ) as mock_super_plan:
                mock_super_plan.return_value = MockChatMessage("Test plan")
                # Don't set task_ledger to simulate the error condition
                self.manager.task_ledger = None

                with self.assertRaises(RuntimeError) as context:
                    await self.manager.plan(self.test_context)

                self.assertIn(
                    "task_ledger not set after plan()", str(context.exception)
                )

    async def test_replan(self):
        """Test replan method."""
        result = await self.manager.replan(self.test_context)

        self.assertIsInstance(result, MockChatMessage)
        self.assertEqual(result.text, "Test replan")

    async def test_create_progress_ledger_normal(self):
        """Test create_progress_ledger with normal round count."""
        # Setup
        context = MockMagenticContext(round_count=5)

        # Execute
        ledger = await self.manager.create_progress_ledger(context)

        # Verify
        self.assertIsNotNone(ledger)
        self.assertFalse(ledger.is_request_satisfied.answer)
        self.assertTrue(ledger.is_in_loop.answer)

    async def test_create_progress_ledger_max_rounds_exceeded(self):
        """Test create_progress_ledger when max rounds exceeded."""
        # Setup
        context = MockMagenticContext(round_count=15)  # Exceeds max_rounds=10

        # Execute
        ledger = await self.manager.create_progress_ledger(context)

        # Verify termination conditions
        self.assertTrue(ledger.is_request_satisfied.answer)
        self.assertEqual(ledger.is_request_satisfied.reason, "Maximum rounds exceeded")
        self.assertFalse(ledger.is_in_loop.answer)
        self.assertEqual(ledger.is_in_loop.reason, "Terminating")
        self.assertFalse(ledger.is_progress_being_made.answer)
        self.assertEqual(
            ledger.instruction_or_question.answer,
            "Process terminated due to maximum rounds exceeded",
        )

        # Verify final message was sent
        connection_config.send_status_update_async.assert_called()

    async def test_prepare_final_answer(self):
        """Test prepare_final_answer method."""
        result = await self.manager.prepare_final_answer(self.test_context)

        self.assertIsInstance(result, MockChatMessage)
        self.assertEqual(result.text, "Final answer")

    def test_plan_to_obj_success(self):
        """Test plan_to_obj with valid ledger."""
        # Setup
        ledger = Mock()
        ledger.plan = Mock()
        ledger.plan.text = "Test plan text"
        ledger.facts = Mock()
        ledger.facts.text = "Test facts text"

        # Execute
        result = self.manager.plan_to_obj(self.test_context, ledger)

        # Verify
        self.assertIsInstance(result, MPlan)

    def test_plan_to_obj_invalid_ledger_none(self):
        """Test plan_to_obj with None ledger."""
        with self.assertRaises(ValueError) as context:
            self.manager.plan_to_obj(self.test_context, None)

        self.assertIn("Invalid ledger structure", str(context.exception))

    def test_plan_to_obj_invalid_ledger_no_plan(self):
        """Test plan_to_obj with ledger missing plan attribute."""
        ledger = Mock()
        del ledger.plan  # Remove plan attribute
        ledger.facts = Mock()

        with self.assertRaises(ValueError) as context:
            self.manager.plan_to_obj(self.test_context, ledger)

        self.assertIn("Invalid ledger structure", str(context.exception))

    def test_plan_to_obj_invalid_ledger_no_facts(self):
        """Test plan_to_obj with ledger missing facts attribute."""
        ledger = Mock()
        ledger.plan = Mock()
        del ledger.facts  # Remove facts attribute

        with self.assertRaises(ValueError) as context:
            self.manager.plan_to_obj(self.test_context, ledger)

        self.assertIn("Invalid ledger structure", str(context.exception))

    def test_plan_to_obj_with_string_task(self):
        """Test plan_to_obj with string task instead of ChatMessage."""
        # Setup
        context = MockMagenticContext(task="String task")
        ledger = Mock()
        ledger.plan = Mock()
        ledger.plan.text = "Test plan text"
        ledger.facts = Mock()
        ledger.facts.text = "Test facts text"

        # Execute
        result = self.manager.plan_to_obj(context, ledger)

        # Verify
        self.assertIsInstance(result, MPlan)

    async def test_plan_context_without_participant_descriptions(self):
        """Test plan method with context missing participant_descriptions."""
        # Setup
        context = MockMagenticContext()
        del context.participant_descriptions  # Remove the attribute

        # Mock the plan_to_obj method to handle missing attribute gracefully
        with patch.object(self.manager, "plan_to_obj") as mock_plan_to_obj:
            mock_plan = MPlan(id="test-plan-id")
            mock_plan_to_obj.return_value = mock_plan

            # Execute - should handle missing participant_descriptions
            result = await self.manager.plan(context)

            # Verify the plan_to_obj was called (showing it got past the participant_descriptions check)
            mock_plan_to_obj.assert_called_once()
            self.assertIsInstance(result, MockChatMessage)

    async def test_plan_with_chat_message_task(self):
        """Test plan method with ChatMessage task."""
        # Setup
        task = MockChatMessage("Test task from ChatMessage")
        context = MockMagenticContext(task=task)

        # Execute
        result = await self.manager.plan(context)

        # Verify
        self.assertIsInstance(result, MockChatMessage)

    def test_approval_enabled_default(self):
        """Test that approval_enabled is True by default."""
        mock_agent = Mock()
        manager = HumanApprovalMagenticManager(
            user_id="test_user", agent=mock_agent
        )

        self.assertTrue(manager.approval_enabled)

    def test_magentic_plan_default(self):
        """Test that magentic_plan is None by default."""
        mock_agent = Mock()
        manager = HumanApprovalMagenticManager(
            user_id="test_user", agent=mock_agent
        )

        self.assertIsNone(manager.magentic_plan)

    async def test_replan_with_none_message(self):
        """Test replan method when super().replan returns None."""
        with patch(
            "v4.orchestration.human_approval_manager.StandardMagenticManager.replan",
            return_value=None,
        ):
            result = await self.manager.replan(self.test_context)
            # Should handle None gracefully
            self.assertIsNone(result)

    async def test_create_progress_ledger_websocket_error(self):
        """Test create_progress_ledger when WebSocket sending fails for max rounds."""
        # Setup
        context = MockMagenticContext(round_count=15)  # Exceeds max_rounds=10

        # Mock websocket failure
        connection_config.send_status_update_async.side_effect = Exception(
            "WebSocket error"
        )

        # Execute - should handle the error gracefully but still raise it
        with self.assertRaises(Exception) as cm:
            await self.manager.create_progress_ledger(context)

        # Verify the exception message
        self.assertEqual(str(cm.exception), "WebSocket error")

        # Reset side effect for other tests
        connection_config.send_status_update_async.side_effect = None


if __name__ == "__main__":
    unittest.main()
