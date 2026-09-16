"""
Comprehensive unit tests for PlanService.

This module contains extensive test coverage for:
- PlanService static methods for handling various message types
- Utility functions for building agent messages
- Plan approval and rejection workflows
- Agent message processing and persistence
- Human clarification handling
- Error handling and edge cases
"""

import pytest
import asyncio
import json
import logging
import v4.common.services.plan_service as plan_service_module
from v4.common.services.plan_service import PlanService
import v4.models.messages as real_messages


@pytest.fixture(autouse=True)
def _collaborators_patched(monkeypatch):
    """Colaboradores de v4.common.services.plan_service parcheados en SU namespace y sólo durante cada
    test. Antes el módulo se cargaba por ruta de archivo con sus dependencias
    sustituidas en sys.modules y se registraba así, con Mocks dentro, para
    todo el proceso (INC-2026-004)."""
    mod = plan_service_module
    for name, value in (
        ('AgentMessageData', mock_messages_af.AgentMessageData),
        ('AgentMessageType', mock_messages_af.AgentMessageType),
        ('AgentType', mock_messages_af.AgentType),
        ('PlanStatus', mock_messages_af.PlanStatus),
        ('messages', mock_v4_messages),
        ('DatabaseFactory', mock_database_factory.DatabaseFactory),
    ):
        monkeypatch.setattr(mod, name, value)

from unittest.mock import MagicMock, AsyncMock
from typing import Any, List
from dataclasses import dataclass


# Mock Azure modules before importing the PlanService
azure_ai_module = MagicMock()
azure_ai_projects_module = MagicMock()
azure_ai_projects_aio_module = MagicMock()

# Create mock AIProjectClient
mock_ai_project_client = MagicMock()
azure_ai_projects_aio_module.AIProjectClient = mock_ai_project_client

# Set up the module hierarchy
azure_ai_module.projects = azure_ai_projects_module
azure_ai_projects_module.aio = azure_ai_projects_aio_module

# Inject the mocked modules

# Mock other problematic modules and imports

# Mock the config module
mock_config_module = MagicMock()
mock_config = MagicMock()

# Mock config attributes for database and other dependencies
mock_config.DATABASE_TYPE = "memory"
mock_config.DATABASE_CONNECTION = "test-connection"

mock_config_module.config = mock_config

# Mock database modules
mock_database_factory = MagicMock()

# Mock event utils
mock_event_utils = MagicMock()

# Create mock message types and enums
mock_messages_af = MagicMock()


# Create mock enums
class MockAgentType:
    HUMAN = MagicMock()
    HUMAN.value = "Human_Agent"


class MockAgentMessageType:
    HUMAN_AGENT = "Human_Agent"
    AI_AGENT = "AI_Agent"


class MockStatusValue(str):
    """String that also exposes .value like an Enum member.

    plan_service now writes PlanStatus.completed.value into plan.m_plan
    (m_plan/agent_message persistence reused from orchestration_manager),
    so mocked statuses must behave like enum members AND compare as strings.
    """

    @property
    def value(self):
        return str(self)


class MockPlanStatus:
    approved = MockStatusValue("approved")
    completed = MockStatusValue("completed")
    rejected = MockStatusValue("rejected")


# Create mock AgentMessageData class
class MockAgentMessageData:
    def __init__(
        self,
        plan_id,
        user_id,
        m_plan_id,
        agent,
        agent_type,
        content,
        raw_data,
        steps,
        next_steps,
    ):
        self.plan_id = plan_id
        self.user_id = user_id
        self.m_plan_id = m_plan_id
        self.agent = agent
        self.agent_type = agent_type
        self.content = content
        self.raw_data = raw_data
        self.steps = steps
        self.next_steps = next_steps


mock_messages_af.AgentType = MockAgentType
mock_messages_af.AgentMessageType = MockAgentMessageType
mock_messages_af.PlanStatus = MockPlanStatus
mock_messages_af.AgentMessageData = MockAgentMessageData

# Create mock v4.models.messages module
mock_v4_messages = MagicMock()

# Now import the real PlanService using direct file import with proper mocking



build_agent_message_from_user_clarification = (
    plan_service_module.build_agent_message_from_user_clarification
)
build_agent_message_from_agent_message_response = (
    plan_service_module.build_agent_message_from_agent_message_response
)


# Test data classes
@dataclass
class MockUserClarificationResponse:
    plan_id: str = ""
    m_plan_id: str = ""
    answer: str = ""


@dataclass
class MockAgentMessageResponse:
    plan_id: str = ""
    user_id: str = ""
    m_plan_id: str = ""
    agent: str = ""
    agent_name: str = ""
    source: str = ""
    agent_type: Any = None
    content: str = ""
    text: str = ""
    raw_data: Any = None
    steps: List = None
    next_steps: List = None
    is_final: bool = False
    streaming_message: str = ""


def _approval(plan_id=None, m_plan_id="", approved=True, feedback=None, decision=None):
    """El modelo real del contrato (dataclass), no un doble: lo que valida el router."""
    return real_messages.PlanApprovalResponse(
        m_plan_id=m_plan_id, approved=approved, feedback=feedback, plan_id=plan_id, decision=decision
    )


class TestUtilityFunctions:
    """Test cases for utility functions."""

    def test_build_agent_message_from_user_clarification_basic(self):
        """Test basic agent message building from user clarification."""
        feedback = MockUserClarificationResponse(
            plan_id="test-plan-123",
            m_plan_id="test-m-plan-456",
            answer="This is my clarification",
        )
        user_id = "test-user-789"

        result = build_agent_message_from_user_clarification(feedback, user_id)

        assert result.plan_id == "test-plan-123"
        assert result.user_id == "test-user-789"
        assert result.m_plan_id == "test-m-plan-456"
        assert result.agent == "Human_Agent"
        assert result.content == "This is my clarification"
        assert result.steps == []
        assert result.next_steps == []

    def test_build_agent_message_from_user_clarification_empty_fields(self):
        """Test building agent message with empty/None fields."""
        feedback = MockUserClarificationResponse(
            plan_id=None, m_plan_id=None, answer=None
        )
        user_id = "test-user"

        result = build_agent_message_from_user_clarification(feedback, user_id)

        assert result.plan_id == ""
        assert result.user_id == "test-user"
        assert result.m_plan_id is None
        assert result.content == ""

    def test_build_agent_message_from_user_clarification_raw_data_serialization(self):
        """Test that raw_data is properly serialized as JSON."""
        feedback = MockUserClarificationResponse(
            plan_id="test-plan", answer="test answer"
        )
        user_id = "test-user"

        result = build_agent_message_from_user_clarification(feedback, user_id)

        # Parse the raw_data JSON to verify it's valid
        raw_data = json.loads(result.raw_data)
        assert raw_data["plan_id"] == "test-plan"
        assert raw_data["answer"] == "test answer"

    def test_build_agent_message_from_agent_message_response_basic(self):
        """Test basic agent message building from agent response."""
        response = MockAgentMessageResponse(
            plan_id="test-plan-123",
            user_id="response-user",
            agent="TestAgent",
            content="Agent response content",
            steps=["step1", "step2"],
            next_steps=["next1"],
        )
        user_id = "fallback-user"

        result = build_agent_message_from_agent_message_response(response, user_id)

        assert result.plan_id == "test-plan-123"
        assert result.user_id == "response-user"  # Should use response user_id
        assert result.agent == "TestAgent"
        assert result.content == "Agent response content"
        assert result.steps == ["step1", "step2"]
        assert result.next_steps == ["next1"]

    def test_build_agent_message_from_agent_message_response_fallbacks(self):
        """Test fallback logic for missing fields."""
        response = MockAgentMessageResponse(
            plan_id="",
            user_id="",
            agent="",
            agent_name="NamedAgent",
            text="Text content",
            steps=None,
            next_steps=None,
        )
        user_id = "fallback-user"

        result = build_agent_message_from_agent_message_response(response, user_id)

        assert result.plan_id == ""
        assert result.user_id == "fallback-user"  # Should use fallback
        assert result.agent == "NamedAgent"  # Should use agent_name fallback
        assert result.content == "Text content"  # Should use text fallback
        assert result.steps == []  # Should default to empty list
        assert result.next_steps == []

    def test_build_agent_message_from_agent_message_response_agent_type_inference(self):
        """Test agent type inference logic."""
        # Test human agent type inference
        response_human = MockAgentMessageResponse(agent_type="human_agent")
        result = build_agent_message_from_agent_message_response(response_human, "user")
        assert result.agent_type == MockAgentMessageType.HUMAN_AGENT

        # Test AI agent type fallback
        response_ai = MockAgentMessageResponse(agent_type="unknown")
        result = build_agent_message_from_agent_message_response(response_ai, "user")
        assert result.agent_type == MockAgentMessageType.AI_AGENT

    def test_build_agent_message_from_agent_message_response_raw_data_handling(self):
        """Test various raw_data handling scenarios."""
        # Test with dict raw_data
        response_dict = MockAgentMessageResponse(raw_data={"test": "data"})
        result = build_agent_message_from_agent_message_response(response_dict, "user")
        assert '"test": "data"' in result.raw_data

        # Test with None raw_data (should use asdict fallback)
        response_none = MockAgentMessageResponse(raw_data=None, content="test")
        result = build_agent_message_from_agent_message_response(response_none, "user")
        # Should contain serialized object data
        assert isinstance(result.raw_data, str)

    def test_build_agent_message_from_agent_message_response_source_fallback(self):
        """Test agent name fallback to source field."""
        response = MockAgentMessageResponse(
            agent="", agent_name="", source="SourceAgent"
        )

        result = build_agent_message_from_agent_message_response(response, "user")
        assert result.agent == "SourceAgent"


class TestPlanService:
    """Test cases for PlanService class."""

    @pytest.mark.asyncio
    async def test_handle_plan_approval_success(self):
        """Approval is recorded on the persisted Plan only (durable across restarts):
        m_plan gets plan_id/team_id/APPROVED, plan gets approved status + flag."""
        mock_approval = _approval(
            plan_id="test-plan-123",
            m_plan_id="test-m-plan-456",
            approved=True,
            feedback="Looks good!",
        )
        mock_plan = MagicMock()
        mock_plan.team_id = "test-team"
        mock_plan.m_plan = None
        mock_plan.waiting_for = {"kind": "plan_review", "m_plan": {"id": "test-m-plan-456"}}
        mock_db = MagicMock()
        mock_db.get_plan_by_plan_id = AsyncMock(return_value=mock_plan)
        mock_db.update_plan = AsyncMock()
        mock_db.delete_plan_by_plan_id = AsyncMock()
        mock_database_factory.DatabaseFactory.get_database = AsyncMock(
            return_value=mock_db
        )

        result = await PlanService.handle_plan_approval(mock_approval, "test-user")

        assert result is True
        mock_db.get_plan_by_plan_id.assert_awaited_once_with(plan_id="test-plan-123")
        assert mock_plan.m_plan["id"] == "test-m-plan-456"
        assert mock_plan.m_plan["plan_id"] == "test-plan-123"
        assert mock_plan.m_plan["team_id"] == "test-team"
        assert mock_plan.overall_status == MockPlanStatus.approved
        assert mock_plan.approved is True
        mock_db.update_plan.assert_awaited_once_with(mock_plan)
    @pytest.mark.asyncio
    async def test_handle_plan_approval_rejection(self):
        """Rejection records nothing here: the caller cancels the parked request
        (OrchestrationManager.cancel_parked). The plan is neither deleted nor mutated."""
        mock_approval = _approval(
            plan_id="test-plan-123",
            m_plan_id="test-m-plan-456",
            approved=False,
            feedback="Need changes",
        )
        mock_plan = MagicMock()
        mock_db = MagicMock()
        mock_db.get_plan_by_plan_id = AsyncMock(return_value=mock_plan)
        mock_db.update_plan = AsyncMock()
        mock_db.delete_plan_by_plan_id = AsyncMock()
        mock_database_factory.DatabaseFactory.get_database = AsyncMock(
            return_value=mock_db
        )

        result = await PlanService.handle_plan_approval(mock_approval, "test-user")

        assert result is True
        mock_db.update_plan.assert_not_called()
        mock_db.delete_plan_by_plan_id.assert_not_called()
    @pytest.mark.asyncio
    async def test_handle_plan_approval_requires_plan_id(self):
        """Without plan_id there is no persisted Plan to record the decision on."""
        mock_approval = _approval(plan_id=None, approved=True)
        mock_db = MagicMock()
        mock_db.get_plan_by_plan_id = AsyncMock(return_value=MagicMock())
        mock_db.update_plan = AsyncMock()
        mock_db.delete_plan_by_plan_id = AsyncMock()
        mock_database_factory.DatabaseFactory.get_database = AsyncMock(
            return_value=mock_db
        )

        result = await PlanService.handle_plan_approval(mock_approval, "user")

        assert result is False
        mock_database_factory.DatabaseFactory.get_database.assert_not_called()
    @pytest.mark.asyncio
    async def test_handle_plan_approval_plan_not_found(self):
        """Test when plan is not found in memory store."""
        mock_approval = _approval(
            plan_id="missing-plan", m_plan_id="test-m-plan", approved=True
        )
        mock_db = MagicMock()
        mock_db.get_plan_by_plan_id = AsyncMock(return_value=None)
        mock_db.update_plan = AsyncMock()
        mock_db.delete_plan_by_plan_id = AsyncMock()
        mock_database_factory.DatabaseFactory.get_database = AsyncMock(
            return_value=mock_db
        )

        result = await PlanService.handle_plan_approval(mock_approval, "user")

        assert result is False
        mock_db.update_plan.assert_not_called()
    @pytest.mark.asyncio
    async def test_handle_plan_approval_exception(self):
        """Store failures are swallowed into False, never raised to the router."""
        mock_approval = _approval(plan_id="plan-1", approved=True)
        mock_database_factory.DatabaseFactory.get_database = AsyncMock(
            side_effect=RuntimeError("cosmos down")
        )

        result = await PlanService.handle_plan_approval(mock_approval, "user")

        assert result is False
    @pytest.mark.asyncio
    async def test_handle_agent_messages_success(self):
        """Test successful agent message handling."""
        mock_message = MockAgentMessageResponse(
            plan_id="test-plan",
            agent="TestAgent",
            content="Agent message content",
            is_final=False,
        )
        user_id = "test-user"

        # Setup mock database
        mock_db = MagicMock()
        mock_db.add_agent_message = AsyncMock()
        mock_database_factory.DatabaseFactory.get_database = AsyncMock(
            return_value=mock_db
        )

        result = await PlanService.handle_agent_messages(mock_message, user_id)

        assert result is True
        mock_db.add_agent_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_agent_messages_final_message(self):
        """Test handling final agent message."""
        mock_message = MockAgentMessageResponse(
            plan_id="test-plan",
            agent="TestAgent",
            content="Final message",
            is_final=True,
            streaming_message="Stream completed",
        )
        user_id = "test-user"

        # Setup mock database and plan
        mock_db = MagicMock()
        mock_plan = MagicMock()
        # m_plan is persisted as a dict; final message also flips its status
        mock_plan.m_plan = {"overall_status": "in_progress"}
        mock_db.add_agent_message = AsyncMock()
        mock_db.get_plan = AsyncMock(return_value=mock_plan)
        mock_db.update_plan = AsyncMock()
        mock_database_factory.DatabaseFactory.get_database = AsyncMock(
            return_value=mock_db
        )

        result = await PlanService.handle_agent_messages(mock_message, user_id)

        assert result is True
        assert mock_plan.streaming_message == "Stream completed"
        assert mock_plan.overall_status == MockPlanStatus.completed
        assert mock_plan.m_plan["overall_status"] == "completed"
        mock_db.update_plan.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_agent_messages_exception(self):
        """Test exception handling in agent message processing."""
        mock_message = MockAgentMessageResponse()

        # Mock database to raise exception
        mock_database_factory.DatabaseFactory.get_database = AsyncMock(
            side_effect=Exception("Database error")
        )

        result = await PlanService.handle_agent_messages(mock_message, "user")

        assert result is False

    @pytest.mark.asyncio
    async def test_handle_human_clarification_success(self):
        """Test successful human clarification handling."""
        mock_clarification = MockUserClarificationResponse(
            plan_id="test-plan", answer="This is my clarification"
        )
        user_id = "test-user"

        # Setup mock database
        mock_db = MagicMock()
        mock_db.add_agent_message = AsyncMock()
        mock_database_factory.DatabaseFactory.get_database = AsyncMock(
            return_value=mock_db
        )

        result = await PlanService.handle_human_clarification(
            mock_clarification, user_id
        )

        assert result is True
        mock_db.add_agent_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_human_clarification_exception(self):
        """Test exception handling in human clarification."""
        mock_clarification = MockUserClarificationResponse()

        # Mock database to raise exception
        mock_database_factory.DatabaseFactory.get_database = AsyncMock(
            side_effect=Exception("Database error")
        )

        result = await PlanService.handle_human_clarification(
            mock_clarification, "user"
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_static_method_properties(self):
        """Test that all PlanService methods are static."""
        mock_approval = _approval(plan_id=None, approved=False)
        result = await PlanService.handle_plan_approval(mock_approval, "user")
        assert result is False
    def test_event_tracking_calls(self):
        """Test that event tracking is callable via the mocked event_utils module."""
        # Verify the mock event_utils has the track function accessible
        assert callable(mock_event_utils.track_event_if_configured)

    def test_logging_integration(self):
        """Test that logging is properly configured."""
        # Verify that the logger is set up correctly
        logger = logging.getLogger("v4.common.services.plan_service")
        assert logger is not None

    @pytest.mark.asyncio
    async def test_integration_scenario_approval_workflow(self):
        """Approval of a parked plan whose m_plan lives only in waiting_for
        (the restart case: nothing in memory, everything in the store)."""
        mock_plan = MagicMock()
        mock_plan.team_id = "team-456"
        mock_plan.m_plan = None
        mock_plan.waiting_for = {
            "kind": "plan_review",
            "request_id": "req-1",
            "m_plan": {"id": "m-plan-123", "steps": []},
        }
        mock_db = MagicMock()
        mock_db.get_plan_by_plan_id = AsyncMock(return_value=mock_plan)
        mock_db.update_plan = AsyncMock()
        mock_db.delete_plan_by_plan_id = AsyncMock()
        mock_database_factory.DatabaseFactory.get_database = AsyncMock(
            return_value=mock_db
        )

        approval = _approval(
            plan_id="plan-123",
            m_plan_id="m-plan-123",
            approved=True,
            feedback="Approved",
        )

        result = await PlanService.handle_plan_approval(approval, "user-123")

        assert result is True
        assert mock_plan.m_plan["plan_id"] == "plan-123"
        assert mock_plan.m_plan["team_id"] == "team-456"
        assert mock_plan.m_plan["steps"] == []
        assert mock_plan.overall_status == MockPlanStatus.approved
    @pytest.mark.asyncio
    async def test_integration_scenario_message_processing(self):
        """Test complete message processing workflow."""
        # Test agent message processing
        mock_db = MagicMock()
        mock_db.add_agent_message = AsyncMock()
        mock_database_factory.DatabaseFactory.get_database = AsyncMock(
            return_value=mock_db
        )

        agent_msg = MockAgentMessageResponse(
            plan_id="plan-456",
            agent="ProcessingAgent",
            content="Processing complete",
            is_final=False,
        )

        result = await PlanService.handle_agent_messages(agent_msg, "user-456")
        assert result is True

        # Test human clarification
        clarification = MockUserClarificationResponse(
            plan_id="plan-456", answer="Additional clarification"
        )

        result = await PlanService.handle_human_clarification(clarification, "user-456")
        assert result is True

        # Verify both calls made it to the database
        assert mock_db.add_agent_message.call_count == 2

    def test_error_resilience(self):
        """Test error handling and resilience across different scenarios."""
        # Test with various malformed inputs
        malformed_inputs = [
            MockUserClarificationResponse(plan_id=None, answer=None),
            MockAgentMessageResponse(plan_id="", content="", steps=[]),
            _approval(approved=True, plan_id=""),
        ]

        for input_obj in malformed_inputs:
            # These should not raise exceptions during object creation
            assert input_obj is not None

    @pytest.mark.asyncio
    async def test_concurrent_operations(self):
        """Test handling of concurrent operations."""
        mock_db = MagicMock()
        mock_db.add_agent_message = AsyncMock()
        mock_database_factory.DatabaseFactory.get_database = AsyncMock(
            return_value=mock_db
        )

        # Create multiple tasks
        tasks = []
        for i in range(5):
            clarification = MockUserClarificationResponse(
                plan_id=f"plan-{i}", answer=f"Clarification {i}"
            )
            task = PlanService.handle_human_clarification(clarification, f"user-{i}")
            tasks.append(task)

        results = await asyncio.gather(*tasks)

        # All should succeed
        assert all(results)
        assert mock_db.add_agent_message.call_count == 5
