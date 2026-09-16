import json
import logging
from dataclasses import asdict

import v4.models.messages as messages
from common.database.database_factory import DatabaseFactory
from common.models.messages_af import (
    AgentMessageData,
    AgentMessageType,
    AgentType,
    PlanStatus,
)
from v4.models.models import PlanStatus as V4PlanStatus

logger = logging.getLogger(__name__)


def build_agent_message_from_user_clarification(
    human_feedback: messages.UserClarificationResponse, user_id: str
) -> AgentMessageData:
    """
    Convert a UserClarificationResponse (human feedback) into an AgentMessageData.
    """
    # NOTE: AgentMessageType enum currently defines values with trailing commas in messages_af.py.
    # e.g. HUMAN_AGENT = "Human_Agent",  -> value becomes ('Human_Agent',)
    # Consider fixing that enum (remove trailing commas) so .value is a string.
    return AgentMessageData(
        plan_id=human_feedback.plan_id or "",
        user_id=user_id,
        m_plan_id=human_feedback.m_plan_id or None,
        agent=AgentType.HUMAN.value,  # or simply "Human_Agent"
        agent_type=AgentMessageType.HUMAN_AGENT,  # will serialize per current enum definition
        content=human_feedback.answer or "",
        raw_data=json.dumps(asdict(human_feedback)),
        steps=[],  # intentionally empty
        next_steps=[],  # intentionally empty
    )


def build_agent_message_from_agent_message_response(
    agent_response: messages.AgentMessageResponse,
    user_id: str,
) -> AgentMessageData:
    """
    Convert a messages.AgentMessageResponse into common.models.messages_af.AgentMessageData.
    This is defensive: it tolerates missing fields and different timestamp formats.
    """
    # Robust timestamp parsing (accepts seconds or ms or missing)

    # Raw data serialization
    raw = getattr(agent_response, "raw_data", None)
    try:
        if raw is None:
            # try asdict if it's a dataclass-like
            try:
                raw_str = json.dumps(asdict(agent_response))
            except Exception:
                raw_str = json.dumps(
                    {
                        k: getattr(agent_response, k)
                        for k in dir(agent_response)
                        if not k.startswith("_")
                    }
                )
        elif isinstance(raw, (dict, list)):
            raw_str = json.dumps(raw)
        else:
            raw_str = str(raw)
    except Exception:
        raw_str = json.dumps({"raw": str(raw)})

    # Steps / next_steps defaulting
    steps = getattr(agent_response, "steps", []) or []
    next_steps = getattr(agent_response, "next_steps", []) or []

    # Agent name and type
    agent_name = (
        getattr(agent_response, "agent", "")
        or getattr(agent_response, "agent_name", "")
        or getattr(agent_response, "source", "")
    )
    # Try to infer agent_type, fallback to AI_AGENT
    agent_type_raw = getattr(agent_response, "agent_type", None)
    if isinstance(agent_type_raw, AgentMessageType):
        agent_type = agent_type_raw
    else:
        # Normalize common strings
        agent_type_str = str(agent_type_raw or "").lower()
        if "human" in agent_type_str:
            agent_type = AgentMessageType.HUMAN_AGENT
        else:
            agent_type = AgentMessageType.AI_AGENT

    # Content
    content = (
        getattr(agent_response, "content", "")
        or getattr(agent_response, "text", "")
        or ""
    )

    # plan_id / user_id fallback
    plan_id_val = getattr(agent_response, "plan_id", "") or ""
    user_id_val = getattr(agent_response, "user_id", "") or user_id

    return AgentMessageData(
        plan_id=plan_id_val,
        user_id=user_id_val,
        m_plan_id=getattr(agent_response, "m_plan_id", ""),
        agent=agent_name,
        agent_type=agent_type,
        content=content,
        raw_data=raw_str,
        steps=list(steps),
        next_steps=list(next_steps),
    )


class PlanService:
    @staticmethod
    async def handle_plan_approval(
        human_feedback: messages.PlanApprovalResponse, user_id: str
    ) -> bool:
        """Record the human's decision on a parked plan review.

        Reads and writes only the persisted ``Plan`` (no in-process registry), so
        the decision is honoured after a restart. Approval marks the plan and its
        ``m_plan`` approved. Rejection records nothing here: the caller cancels
        the parked request via ``OrchestrationManager.cancel_parked``.

        Returns:
            True when the decision was recorded, False otherwise.
        """
        plan_id_val = human_feedback.plan_id
        if not plan_id_val:
            return False
        try:
            memory_store = await DatabaseFactory.get_database(user_id=user_id)
            plan = await memory_store.get_plan_by_plan_id(plan_id=plan_id_val)
            if plan is None:
                logger.warning("Plan %s not found in memory store.", plan_id_val)
                return False
            if not human_feedback.approved:
                return True
            m_plan = dict(plan.m_plan or (plan.waiting_for or {}).get("m_plan") or {})
            m_plan["plan_id"] = plan_id_val
            m_plan["team_id"] = plan.team_id or ""
            m_plan["overall_status"] = V4PlanStatus.APPROVED.value
            plan.m_plan = m_plan
            plan.overall_status = PlanStatus.approved
            plan.approved = True  # keep boolean field consistent with overall_status
            await memory_store.update_plan(plan)
        except Exception as e:
            logger.error("Error processing plan approval: %s", e)
            return False
        return True

    @staticmethod
    async def handle_agent_messages(
        agent_message: messages.AgentMessageResponse, user_id: str
    ) -> bool:
        """
        Process an AgentMessage coming from the client.

        Args:
            standard_message: messages.AgentMessage (contains relevant message data)
            user_id: authenticated user id

        Returns:
            dict with status and metadata

        Raises:
            ValueError on invalid state
        """
        try:
            agent_msg = build_agent_message_from_agent_message_response(
                agent_message, user_id
            )

            # Persist if your database layer supports it.
            # Look for or implement something like: memory_store.add_agent_message(agent_msg)
            memory_store = await DatabaseFactory.get_database(user_id=user_id)
            await memory_store.add_agent_message(agent_msg)
            if agent_message.is_final:
                plan = await memory_store.get_plan(agent_msg.plan_id)
                if plan is not None:
                    plan.streaming_message = agent_message.streaming_message
                    plan.overall_status = PlanStatus.completed
                    if plan.m_plan:
                        plan.m_plan["overall_status"] = PlanStatus.completed.value
                    await memory_store.update_plan(plan)
            return True
        except Exception as e:
            logger.exception(
                "Failed to handle human clarification -> agent message: %s", e
            )
            return False

    @staticmethod
    async def handle_human_clarification(
        human_feedback: messages.UserClarificationResponse, user_id: str
    ) -> bool:
        """
        Process a UserClarificationResponse coming from the client.

        Args:
            human_feedback: messages.UserClarificationResponse (contains relevant message data)
            user_id: authenticated user id

        Returns:
            dict with status and metadata

        Raises:
            ValueError on invalid state
        """
        try:
            agent_msg = build_agent_message_from_user_clarification(
                human_feedback, user_id
            )

            # Persist if your database layer supports it.
            # Look for or implement something like: memory_store.add_agent_message(agent_msg)
            memory_store = await DatabaseFactory.get_database(user_id=user_id)
            await memory_store.add_agent_message(agent_msg)

            return True
        except Exception as e:
            logger.exception(
                "Failed to handle human clarification -> agent message: %s", e
            )
            return False
