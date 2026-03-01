import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, cast

from context.cosmos_memory_kernel import CosmosMemoryContext
from event_utils import track_event_if_configured
from kernel_agents.agent_base import BaseAgent
from utils_date import format_date_for_user
from models.messages_kernel import (ActionRequest, AgentMessage, AgentType,
                                    HumanFeedback, HumanFeedbackStatus, InputTask,
                                    Plan, Step, StepStatus)
# pylint: disable=E0611
from semantic_kernel.functions.kernel_function import KernelFunction


class GroupChatManager(BaseAgent):
    """GroupChatManager agent implementation using Semantic Kernel.

    This agent creates and manages plans based on user tasks, breaking them down into steps
    that can be executed by specialized agents to achieve the user's goal.
    """

    def __init__(
        self,
        session_id: str,
        user_id: str,
        memory_store: CosmosMemoryContext,
        tools: Optional[List[KernelFunction]] = None,
        system_message: Optional[str] = None,
        agent_name: str = AgentType.GROUP_CHAT_MANAGER.value,
        agent_tools_list: Optional[List[str]] = None,
        agent_instances: Optional[Dict[str, BaseAgent]] = None,
        client=None,
        definition=None,
    ) -> None:
        """Initialize the GroupChatManager Agent.

        Args:
            session_id: The current session identifier
            user_id: The user identifier
            memory_store: The Cosmos memory context
            system_message: Optional system message for the agent
            agent_name: Optional name for the agent (defaults to "GroupChatManagerAgent")
            config_path: Optional path to the configuration file
            available_agents: List of available agent names for creating steps
            agent_tools_list: List of available tools across all agents
            agent_instances: Dictionary of agent instances available to the GroupChatManager
            client: Optional client instance (passed to BaseAgent)
            definition: Optional definition instance (passed to BaseAgent)
        """
        # Default system message if not provided
        if not system_message:
            system_message = self.default_system_message(agent_name)

        # Initialize the base agent
        super().__init__(
            agent_name=agent_name,
            session_id=session_id,
            user_id=user_id,
            memory_store=memory_store,
            tools=tools,
            system_message=system_message,
            client=client,
            definition=definition,
        )

        # Store additional GroupChatManager-specific attributes
        self._available_agents = [
            AgentType.HUMAN.value,
            AgentType.HR.value,
            AgentType.MARKETING.value,
            AgentType.PRODUCT.value,
            AgentType.PROCUREMENT.value,
            AgentType.TECH_SUPPORT.value,
            AgentType.GENERIC.value,
        ]
        self._agent_tools_list = agent_tools_list or []
        self._agent_instances = agent_instances or {}

        # Create the Azure AI Agent for group chat operations
        # This will be initialized in async_init
        self._azure_ai_agent = None

    @classmethod
    async def create(
        cls,
        **kwargs: Any,
    ) -> "GroupChatManager":
        """Asynchronously create the PlannerAgent.

        Creates the Azure AI Agent for planning operations.

        Returns:
            GroupChatManager instance
        """

        session_id: str = kwargs.get("session_id", "")
        user_id: str = kwargs.get("user_id", "")
        memory_store_value = kwargs.get("memory_store")
        if memory_store_value is None:
            raise ValueError("memory_store is required and cannot be None")
        memory_store: CosmosMemoryContext = memory_store_value
        tools: Optional[List[KernelFunction]] = kwargs.get("tools", None)
        system_message: Optional[str] = kwargs.get("system_message", None)
        agent_name: str = kwargs.get("agent_name", AgentType.GROUP_CHAT_MANAGER.value)
        agent_tools_list = kwargs.get("agent_tools_list", None)
        agent_instances = kwargs.get("agent_instances", None)
        client = kwargs.get("client")

        # Use default system message if not provided
        instructions = system_message or cls.default_system_message(agent_name)

        try:
            logging.info("Initializing GroupChatAgent from async init azure AI Agent")

            # Create the Azure AI Agent using AppConfig with string instructions
            agent_definition = await cls._create_azure_ai_agent_definition(
                agent_name=agent_name,
                instructions=instructions,  # Pass the formatted string, not an object
                tools=tools,  # Pass the tools to the agent definition
                temperature=0.0,
                response_format=None,
            )

            return cls(
                session_id=session_id,
                user_id=user_id,
                memory_store=memory_store,
                tools=tools,
                system_message=system_message,
                agent_name=agent_name,
                agent_tools_list=agent_tools_list,
                agent_instances=agent_instances,
                client=client,
                definition=agent_definition,
            )

        except Exception as e:
            logging.error(f"Failed to create Azure AI Agent for PlannerAgent: {e}")
            raise

    @staticmethod
    def default_system_message(agent_name=None) -> str:
        """Get the default system message for the agent.
        Args:
            agent_name: The name of the agent (optional)
        Returns:
            The default system message for the agent
        """
        return "You are a GroupChatManager agent responsible for creating and managing plans. You analyze tasks, break them down into steps, and assign them to the appropriate specialized agents."

    async def handle_input_task(self, message: InputTask) -> Plan:
        """
        Handles the input task from the user. This is the initial message that starts the conversation.
        This method should create a new plan.
        """
        logging.info(f"Received input task: {message}")
        await self._memory_store.add_item(
            AgentMessage(
                data_type="agent_message",
                session_id=message.session_id,
                user_id=self._user_id,
                plan_id="",
                content=f"{message.description}",
                source=AgentType.HUMAN.value,
                step_id="",
            )
        )

        track_event_if_configured(
            "Group Chat Manager - Received and added input task into the cosmos",
            {
                "session_id": message.session_id,
                "user_id": self._user_id,
                "content": message.description,
                "source": AgentType.HUMAN.value,
            },
        )

        # Send the InputTask to the PlannerAgent
        planner_agent = cast(Any, self._agent_instances[AgentType.PLANNER.value])
        result = await planner_agent.handle_input_task(message)
        logging.info(f"Plan created: {result}")
        return result

    async def handle_human_feedback(self, message: HumanFeedback) -> None:
        """
        Handles the human approval feedback for a single step or all steps.
        Updates the step status and stores the feedback in the session context.

        class HumanFeedback(BaseModel):
            step_id: str
            plan_id: str
            session_id: str
            approved: bool
            human_feedback: Optional[str] = None
            updated_action: Optional[str] = None

        class Step(BaseDataModel):

            data_type: Literal["step"] = Field("step", Literal=True)
            plan_id: str
            action: str
            agent: BAgentType
            status: StepStatus = StepStatus.planned
            agent_reply: Optional[str] =None
            human_feedback: Optional[str] = None
            human_approval_status: Optional[HumanFeedbackStatus] = HumanFeedbackStatus.requested
            updated_action: Optional[str] = None
            session_id: (
                str  # Added session_id to the Step model to partition the steps by session_id
            )
            ts: Optional[int] = None
        """
        # Need to retrieve all the steps for the plan
        logging.info(f"GroupChatManager Received human feedback: {message}")

        steps: List[Step] = await self._memory_store.get_steps_by_plan(message.plan_id)
        # Filter for steps that are planned or awaiting feedback

        # Get the first step assigned to HumanAgent for feedback
        human_feedback_step: Optional[Step] = next(
            (s for s in steps if s.agent == AgentType.HUMAN), None
        )

        # Determine the feedback to use: prefer the current message feedback first
        current_feedback = (message.human_feedback or "").strip()
        if current_feedback:
            received_human_feedback_on_step = current_feedback
        elif human_feedback_step and human_feedback_step.human_feedback:
            received_human_feedback_on_step = human_feedback_step.human_feedback
        else:
            received_human_feedback_on_step = ""

        # Provide generic context to the model
        current_date = datetime.now().strftime("%Y-%m-%d")
        formatted_date = format_date_for_user(current_date)
        general_information = f"Today's date is {formatted_date}."

        # Get the general background information provided by the user in regards to the overall plan (not the steps) to add as context.
        plan = await self._memory_store.get_plan_by_session(
            session_id=message.session_id
        )
        if plan and plan.human_clarification_response:
            received_human_feedback_on_plan = (
                f"{plan.human_clarification_request}: {plan.human_clarification_response}"
                + " This information may or may not be relevant to the step you are executing - it was feedback provided by the human user on the overall plan, which includes multiple steps, not just the one you are actioning now."
            )
        else:
            received_human_feedback_on_plan = (
                "No human feedback provided on the overall plan."
            )
        # Combine all feedback into a single string
        received_human_feedback = (
            f"{received_human_feedback_on_step} "
            f"{general_information} "
            f"{received_human_feedback_on_plan}"
        )

        # Update and execute the specific step if step_id is provided
        if message.step_id:
            step = next((s for s in steps if s.id == message.step_id), None)
            if step:
                await self._update_step_status(
                    step, message.approved, received_human_feedback
                )
                if message.approved:
                    await self._execute_step(message.session_id, step)
                else:
                    # Notify the GroupChatManager that the step has been rejected
                    # TODO: Implement this logic later
                    step.status = StepStatus.rejected
                    step.human_approval_status = HumanFeedbackStatus.rejected
                    await self._memory_store.update_step(step)
                    track_event_if_configured(
                        "Group Chat Manager - Steps has been rejected and updated into the cosmos",
                        {
                            "status": StepStatus.rejected,
                            "session_id": message.session_id,
                            "user_id": self._user_id,
                            "human_approval_status": HumanFeedbackStatus.rejected,
                            "source": step.agent,
                        },
                    )
        else:
            # If this is conversational follow-up (text present, no explicit step_id),
            # route to the most recent non-human execution step to preserve continuity.
            if (message.human_feedback or "").strip():
                ordered_steps = sorted(
                    steps,
                    key=lambda s: (
                        getattr(s, "timestamp", None) is None,
                        str(getattr(s, "timestamp", "")),
                    ),
                )
                target_step = next(
                    (
                        s
                        for s in reversed(ordered_steps)
                        if s.agent
                        not in {
                            AgentType.HUMAN,
                            AgentType.PLANNER,
                            AgentType.GROUP_CHAT_MANAGER,
                        }
                    ),
                    None,
                )
                if target_step:
                    # Create a new follow-up step so the latest user request becomes
                    # the actionable step text (instead of replaying prior action).
                    follow_up_step = Step(
                        plan_id=target_step.plan_id,
                        session_id=target_step.session_id,
                        user_id=target_step.user_id,
                        action=(message.updated_action or message.human_feedback or "").strip(),
                        agent=target_step.agent,
                        status=StepStatus.planned,
                        planner_rationale="Follow-up user message routed in active plan context.",
                    )
                    await self._memory_store.add_step(follow_up_step)

                    await self._update_step_status(
                        follow_up_step, message.approved, received_human_feedback
                    )
                    if message.approved:
                        await self._execute_step(message.session_id, follow_up_step)
                    else:
                        follow_up_step.status = StepStatus.rejected
                        follow_up_step.human_approval_status = HumanFeedbackStatus.rejected
                        await self._memory_store.update_step(follow_up_step)
                    return

            # Legacy behavior: update/execute all steps when no explicit step_id and no follow-up text.
            for step in steps:
                await self._update_step_status(
                    step, message.approved, received_human_feedback
                )
                if message.approved:
                    await self._execute_step(message.session_id, step)
                else:
                    # Notify the GroupChatManager that the step has been rejected
                    # TODO: Implement this logic later
                    step.status = StepStatus.rejected
                    step.human_approval_status = HumanFeedbackStatus.rejected
                    await self._memory_store.update_step(step)
                    track_event_if_configured(
                        f"{AgentType.GROUP_CHAT_MANAGER.value} - Step has been rejected and updated into the cosmos",
                        {
                            "status": StepStatus.rejected,
                            "session_id": message.session_id,
                            "user_id": self._user_id,
                            "human_approval_status": HumanFeedbackStatus.rejected,
                            "source": step.agent,
                        },
                    )

    # Function to update step status and add feedback
    async def _update_step_status(
        self, step: Step, approved: bool, received_human_feedback: str
    ):
        if approved:
            step.status = StepStatus.approved
            step.human_approval_status = HumanFeedbackStatus.accepted
        else:
            step.status = StepStatus.rejected
            step.human_approval_status = HumanFeedbackStatus.rejected

        step.human_feedback = received_human_feedback
        await self._memory_store.update_step(step)
        track_event_if_configured(
            f"{AgentType.GROUP_CHAT_MANAGER.value} - Received human feedback, Updating step and updated into the cosmos",
            {
                "status": step.status,
                "session_id": step.session_id,
                "user_id": self._user_id,
                "human_feedback": received_human_feedback,
                "source": step.agent,
            },
        )

    @staticmethod
    def _extract_function_name_from_action(action: str) -> Optional[str]:
        """Extract function name from planner action text: '... Function: function_name'."""
        match = re.search(r"Function:\s*([a-zA-Z0-9_]+)", action or "")
        return match.group(1) if match else None

    def _build_function_owner_map(self) -> Dict[str, AgentType]:
        """Build a map of function_name -> owning agent when ownership is unique."""
        owner_candidates: Dict[str, set[AgentType]] = {}
        for agent_key, agent_instance in self._agent_instances.items():
            if agent_key in {
                AgentType.PLANNER.value,
                AgentType.GROUP_CHAT_MANAGER.value,
                AgentType.HUMAN.value,
            }:
                continue

            try:
                agent_type = AgentType(agent_key)
            except Exception:
                continue

            tools = getattr(agent_instance, "_tools", []) or []
            for tool in tools:
                metadata = getattr(tool, "metadata", None)
                tool_name = (
                    getattr(metadata, "name", None)
                    or getattr(tool, "name", None)
                    or getattr(tool, "function_name", None)
                )
                if not tool_name:
                    continue
                owner_candidates.setdefault(str(tool_name), set()).add(agent_type)

        owner_map: Dict[str, AgentType] = {}
        for function_name, owners in owner_candidates.items():
            if len(owners) == 1:
                owner_map[function_name] = next(iter(owners))
        return owner_map

    def _extract_function_mentions_from_action(
        self, action: str, known_functions: set[str]
    ) -> List[str]:
        """Extract mentioned function names from free-form planner actions."""
        candidates: List[str] = []

        explicit = self._extract_function_name_from_action(action)
        if explicit:
            candidates.append(explicit)

        # Capture snake_case tokens commonly used as function names.
        for token in re.findall(r"\b[a-z][a-z0-9_]{2,}\b", action or ""):
            if token in known_functions:
                candidates.append(token)

        # Preserve order, remove duplicates.
        deduped: List[str] = []
        seen = set()
        for name in candidates:
            if name not in seen:
                deduped.append(name)
                seen.add(name)
        return deduped

    def _resolve_agent_by_function(self, function_name: str) -> Optional[AgentType]:
        """Resolve the best agent by registered tool/function name, without hardcoded intent rules."""
        owners: List[AgentType] = []
        for agent_key, agent_instance in self._agent_instances.items():
            if agent_key in {
                AgentType.PLANNER.value,
                AgentType.GROUP_CHAT_MANAGER.value,
                AgentType.HUMAN.value,
            }:
                continue
            tools = getattr(agent_instance, "_tools", []) or []
            for tool in tools:
                metadata = getattr(tool, "metadata", None)
                name = getattr(metadata, "name", None)
                if name == function_name:
                    owners.append(AgentType(agent_key))
                    break

        if len(owners) == 1:
            return owners[0]
        return None

    async def _execute_step(self, session_id: str, step: Step):
        """
        Executes the given step by sending an ActionRequest to the appropriate agent.
        """
        function_owner_map = self._build_function_owner_map()
        known_functions = set(function_owner_map.keys())
        mentioned_functions = self._extract_function_mentions_from_action(
            step.action, known_functions
        )
        for function_name in mentioned_functions:
            resolved_agent = function_owner_map.get(function_name)
            if resolved_agent and resolved_agent != step.agent:
                logging.warning(
                    "Planner assigned %s for function %s; rerouting to %s.",
                    step.agent.value,
                    function_name,
                    resolved_agent.value,
                )
                step.agent = resolved_agent
                break

        # Update step status to 'action_requested'
        step.status = StepStatus.action_requested
        await self._memory_store.update_step(step)
        track_event_if_configured(
            f"{AgentType.GROUP_CHAT_MANAGER.value} - Update step to action_requested and updated into the cosmos",
            {
                "status": StepStatus.action_requested,
                "session_id": step.session_id,
                "user_id": self._user_id,
                "source": step.agent,
            },
        )

        # generate conversation history for the invoked agent
        plan = await self._memory_store.get_plan_by_session(session_id=session_id)
        if plan is None:
            raise ValueError(f"No plan found for session {session_id}")
        steps: List[Step] = await self._memory_store.get_steps_by_plan(plan.id)

        current_step_id = step.id
        # Initialize the formatted string
        formatted_string = ""
        formatted_string += "<conversation_history>Here is the conversation history so far for the current plan. This information may or may not be relevant to the step you have been asked to execute."
        formatted_string += f"The user's task was:\n{plan.summary}\n\n"
        formatted_string += (
            f" human_clarification_request:\n{plan.human_clarification_request}\n\n"
        )
        formatted_string += (
            f" human_clarification_response:\n{plan.human_clarification_response}\n\n"
        )
        formatted_string += (
            "The conversation between the previous agents so far is below:\n"
        )

        # Iterate over the steps until the current_step_id
        for i, prior_step in enumerate(steps):
            if prior_step.id == current_step_id:
                break
            formatted_string += f"Step {i}\n"
            formatted_string += (
                f"{AgentType.GROUP_CHAT_MANAGER.value}: {prior_step.action}\n"
            )
            formatted_string += (
                f"{prior_step.agent.value}: {prior_step.agent_reply}\n"
            )
        formatted_string += "<conversation_history \\>"

        logging.info(f"Formatted string: {formatted_string}")

        action_with_history = f"{formatted_string}. Here is the step to action: {step.action}. ONLY perform the steps and actions required to complete this specific step, the other steps have already been completed. Only use the conversational history for additional information, if it's required to complete the step you have been assigned."

        # Send action request to the appropriate agent
        action_request = ActionRequest(
            step_id=step.id,
            plan_id=step.plan_id,
            session_id=session_id,
            action=action_with_history,
            agent=step.agent,
        )
        logging.info(f"Sending ActionRequest to {step.agent.value}")

        agent_name = step.agent.value
        formatted_agent = agent_name.replace("_", " ")

        await self._memory_store.add_item(
            AgentMessage(
                data_type="agent_message",
                session_id=session_id,
                user_id=self._user_id,
                plan_id=step.plan_id,
                content=f"Requesting {formatted_agent} to perform action: {step.action}",
                source=AgentType.GROUP_CHAT_MANAGER.value,
                step_id=step.id,
            )
        )

        track_event_if_configured(
            f"{AgentType.GROUP_CHAT_MANAGER.value} - Requesting {formatted_agent} to perform the action and added into the cosmos",
            {
                "session_id": session_id,
                "user_id": self._user_id,
                "plan_id": step.plan_id,
                "content": f"Requesting {formatted_agent} to perform action: {step.action}",
                "source": AgentType.GROUP_CHAT_MANAGER.value,
                "step_id": step.id,
            },
        )

        if step.agent == AgentType.HUMAN:
            # we mark the step as complete since we have received the human feedback
            # Update step status to 'completed'
            step.status = StepStatus.completed
            await self._memory_store.update_step(step)
            logging.info(
                "Marking the step as complete - Since we have received the human feedback"
            )
            track_event_if_configured(
                "Group Chat Manager - Steps completed - Received the human feedback and updated into the cosmos",
                {
                    "session_id": session_id,
                    "user_id": self._user_id,
                    "plan_id": step.plan_id,
                    "content": "Marking the step as complete - Since we have received the human feedback",
                    "source": step.agent,
                    "step_id": step.id,
                },
            )
        else:
            # Use the agent from the step to determine which agent to send to
            agent = self._agent_instances[step.agent.value]
            await agent.handle_action_request(
                action_request
            )  # this function is in base_agent.py
            logging.info(f"Sent ActionRequest to {step.agent.value}")
