import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple, cast

from azure.ai.agents.models import (ResponseFormatJsonSchema,
                                    ResponseFormatJsonSchemaType)
from context.cosmos_memory_kernel import CosmosMemoryContext
from utils.pii_redactor import get_pii_context
from event_utils import track_event_if_configured
from kernel_agents.agent_base import BaseAgent
from kernel_agents.validator_agent import ValidatorAgent
from kernel_tools.generic_tools import GenericTools
from kernel_tools.hr_tools import HrTools
from kernel_tools.marketing_tools import MarketingTools
from kernel_tools.procurement_tools import ProcurementTools
from kernel_tools.product_tools import ProductTools
from kernel_tools.tech_support_tools import TechSupportTools
from models.messages_kernel import (
    AgentMessage,
    AgentType,
    HumanFeedbackStatus,
    InputTask,
    Plan,
    PlannerResponsePlan,
    PlanStatus,
    Step,
    StepStatus,
)
from semantic_kernel.kernel_pydantic import KernelBaseModel
from semantic_kernel.functions import KernelFunction
from semantic_kernel.functions.kernel_arguments import KernelArguments


class ClarificationEvaluation(KernelBaseModel):
    """Structured result for evaluating a human clarification reply."""

    clarification_resolved: bool
    assistant_message: str
    refined_clarification_request: Optional[str] = None


class PlannerAgent(BaseAgent):
    """Planner agent implementation using Semantic Kernel.

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
        agent_name: str = AgentType.PLANNER.value,
        available_agents: Optional[List[str]] = None,
        agent_instances: Optional[Dict[str, BaseAgent]] = None,
        client=None,
        definition=None,
    ) -> None:
        """Initialize the Planner Agent.

        Args:
            session_id: The current session identifier
            user_id: The user identifier
            memory_store: The Cosmos memory context
            tools: Optional list of tools for this agent
            system_message: Optional system message for the agent
            agent_name: Optional name for the agent (defaults to "PlannerAgent")
            config_path: Optional path to the configuration file
            available_agents: List of available agent names for creating steps
            agent_tools_list: List of available tools across all agents
            agent_instances: Dictionary of agent instances available to the planner
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

        # Store additional planner-specific attributes
        self._available_agents = available_agents or [
            AgentType.HUMAN.value,
            AgentType.HR.value,
            AgentType.MARKETING.value,
            AgentType.PRODUCT.value,
            AgentType.PROCUREMENT.value,
            AgentType.TECH_SUPPORT.value,
            AgentType.GENERIC.value,
        ]
        self._agent_tools_list = {
            AgentType.HR.value: HrTools.generate_tools_json_doc(),
            AgentType.MARKETING.value: MarketingTools.generate_tools_json_doc(),
            AgentType.PRODUCT.value: ProductTools.generate_tools_json_doc(),
            AgentType.PROCUREMENT.value: ProcurementTools.generate_tools_json_doc(),
            AgentType.TECH_SUPPORT.value: TechSupportTools.generate_tools_json_doc(),
            AgentType.GENERIC.value: GenericTools.generate_tools_json_doc(),
        }
        self._merge_runtime_tool_context()

        self._agent_instances = agent_instances or {}

    def _merge_runtime_tool_context(self) -> None:
        """Append runtime (project) tools to planner context for better routing."""
        runtime_tools = self._tools or []
        runtime_tool_names: list[str] = []
        for tool in runtime_tools:
            metadata = getattr(tool, "metadata", None)
            name = (
                getattr(metadata, "name", None)
                or getattr(tool, "name", None)
                or getattr(tool, "function_name", None)
            )
            if name:
                runtime_tool_names.append(str(name))

        if not runtime_tool_names:
            return

        tech_support_tools_context = self._agent_tools_list.get(
            AgentType.TECH_SUPPORT.value, "[]"
        )
        runtime_context = (
            "\nRuntime project tools available (dynamic): "
            + ", ".join(sorted(set(runtime_tool_names)))
        )
        self._agent_tools_list[AgentType.TECH_SUPPORT.value] = (
            f"{tech_support_tools_context}{runtime_context}"
        )

    @staticmethod
    def default_system_message(agent_name=None) -> str:
        """Get the default system message for the agent.
        Args:
            agent_name: The name of the agent (optional)
        Returns:
            The default system message for the agent
        """
        return "You are a Planner agent responsible for creating and managing plans. You analyze tasks, break them down into steps, and assign them to the appropriate specialized agents."

    @classmethod
    async def create(
        cls,
        **kwargs: Any,
    ) -> "PlannerAgent":
        """Asynchronously create the PlannerAgent.

        Creates the Azure AI Agent for planning operations.

        Returns:
            None
        """

        session_id = cast(Optional[str], kwargs.get("session_id"))
        user_id = cast(Optional[str], kwargs.get("user_id"))
        memory_store = cast(Optional[CosmosMemoryContext], kwargs.get("memory_store"))
        tools = cast(Optional[List[KernelFunction]], kwargs.get("tools", None))
        system_message = cast(Optional[str], kwargs.get("system_message", None))
        agent_name = cast(str, kwargs.get("agent_name", AgentType.PLANNER.value))
        available_agents = cast(Optional[List[str]], kwargs.get("available_agents", None))
        agent_instances = cast(Optional[Dict[str, BaseAgent]], kwargs.get("agent_instances", None))
        client = kwargs.get("client")

        if not session_id or not user_id or memory_store is None:
            raise ValueError("session_id, user_id, and memory_store are required")

        # Create the instruction template

        try:
            logging.info("Initializing PlannerAgent from async init azure AI Agent")

            # Create the Azure AI Agent using AppConfig with string instructions
            agent_definition = await cls._create_azure_ai_agent_definition(
                agent_name=agent_name,
                instructions=cls._get_template(),
                temperature=0.0,
                response_format=ResponseFormatJsonSchemaType(
                    json_schema=ResponseFormatJsonSchema(
                        name=PlannerResponsePlan.__name__,
                        description=f"respond with {PlannerResponsePlan.__name__.lower()}",
                        schema=PlannerResponsePlan.model_json_schema(),
                    )
                ),
            )

            return cls(
                session_id=session_id,
                user_id=user_id,
                memory_store=memory_store,
                tools=tools,
                system_message=system_message,
                agent_name=agent_name,
                available_agents=available_agents,
                agent_instances=agent_instances,
                client=client,
                definition=agent_definition,
            )

        except Exception as e:
            logging.error(f"Failed to create Azure AI Agent for PlannerAgent: {e}")
            raise

    async def handle_input_task(self, input_task: InputTask) -> str:
        """Handle the initial input task from the user.

        Args:
            kernel_arguments: Contains the input_task_json string

        Returns:
            Status message
        """
        # Parse the input task
        logging.info("Handling input task")

        # Redact PII from the description to avoid content filter issues
        # The original values are stored in the session's PII context for later re-hydration
        pii_context = get_pii_context(input_task.session_id)
        original_description = input_task.description
        redacted_description = pii_context.redact(original_description)

        if redacted_description != original_description:
            logging.info(f"PII redacted from input: {len(pii_context.get_token_map())} tokens created")
            # Create a modified input task with redacted description
            input_task = InputTask(
                session_id=input_task.session_id,
                description=redacted_description
            )

        plan, steps = await self._create_structured_plan(input_task)

        logging.info(f"Plan created: {plan}")
        logging.info(f"Steps created: {steps}")

        if steps:
            # Add a message about the created plan
            await self._memory_store.add_item(
                AgentMessage(
                    data_type="agent_message",
                    session_id=input_task.session_id,
                    user_id=self._user_id,
                    plan_id=plan.id,
                    content=f"Generated a plan with {len(steps)} steps. Click the checkmark beside each step to complete it, click the x to reject this step.",
                    source=AgentType.PLANNER.value,
                    step_id="",
                )
            )

            track_event_if_configured(
                f"Planner - Generated a plan with {len(steps)} steps and added plan into the cosmos",
                {
                    "session_id": input_task.session_id,
                    "user_id": self._user_id,
                    "plan_id": plan.id,
                    "content": f"Generated a plan with {len(steps)} steps. Click the checkmark beside each step to complete it, click the x to reject this step.",
                    "source": AgentType.PLANNER.value,
                },
            )

            # If human clarification is needed, add a message requesting it
            if (
                hasattr(plan, "human_clarification_request")
                and plan.human_clarification_request
            ):
                await self._memory_store.add_item(
                    AgentMessage(
                        data_type="agent_message",
                        session_id=input_task.session_id,
                        user_id=self._user_id,
                        plan_id=plan.id,
                        content=f"I require additional information before we can proceed: {plan.human_clarification_request}",
                        source=AgentType.PLANNER.value,
                        step_id="",
                    )
                )

                track_event_if_configured(
                    "Planner - Additional information requested and added into the cosmos",
                    {
                        "session_id": input_task.session_id,
                        "user_id": self._user_id,
                        "plan_id": plan.id,
                        "content": f"I require additional information before we can proceed: {plan.human_clarification_request}",
                        "source": AgentType.PLANNER.value,
                    },
                )

        return f"Plan '{plan.id}' created successfully with {len(steps)} steps"

    async def handle_plan_clarification(self, kernel_arguments: KernelArguments) -> str:
        """Handle human clarification for a plan.

        Args:
            kernel_arguments: Contains session_id and human_clarification

        Returns:
            Status message
        """
        session_id = kernel_arguments["session_id"]
        human_clarification = kernel_arguments["human_clarification"]

        # Retrieve and update the plan
        plan = await self._memory_store.get_plan_by_session(session_id)
        if not plan:
            return f"No plan found for session {session_id}"

        # Populate template variables required by planner instructions to avoid
        # unresolved template warnings during invoke.
        template_args = self._generate_args(plan.initial_goal)
        for key in ("objective", "agents_str", "tools_str"):
            value = kernel_arguments.get(key)
            if isinstance(value, str) and value.strip():
                template_args[key] = value

        clarification_request = plan.human_clarification_request or ""
        agents_context = template_args.get("agents_str", "")
        tools_context = template_args.get("tools_str", "")
        evaluation_prompt = (
            "You are the planner in a multi-agent system.\n"
            "Given the original goal, the clarification request asked to the user, and the user's latest reply:\n"
            "1) Decide whether the clarification request is resolved.\n"
            "2) If resolved, provide a concise assistant message with meaningful next guidance.\n"
            "3) If unresolved, provide a concise contextual follow-up question.\n"
            "4) Optionally refine the clarification request if needed.\n\n"
            "Behavior constraints:\n"
            "- Do not return generic acknowledgements such as 'Thanks. The plan has been updated.'\n"
            "- Always provide a substantive response tailored to the user's message.\n"
            "- If no clarification_request exists, treat the user message as a free-form follow-up and respond helpfully using the plan context.\n"
            "- If the user asks a question, answer it directly and clearly.\n\n"
            f"Goal: {plan.initial_goal}\n"
            f"Available agents: {agents_context}\n"
            f"Available tools by agent: {tools_context}\n"
            f"Clarification request: {clarification_request}\n"
            f"User reply: {human_clarification}"
        )

        response_content = ""
        async_generator = self.invoke(
            messages=evaluation_prompt,
            thread=None,
            polling_options=self._get_polling_options(),
            response_format=ResponseFormatJsonSchemaType(
                json_schema=ResponseFormatJsonSchema(
                    name=ClarificationEvaluation.__name__,
                    description="Evaluate if clarification is resolved and produce the next assistant message.",
                    schema=ClarificationEvaluation.model_json_schema(),
                )
            ),
        )
        async for chunk in async_generator:
            if chunk is not None:
                response_content += str(chunk)

        parsed = ClarificationEvaluation.model_validate_json(response_content)

        if parsed.clarification_resolved:
            # Exit clarification mode to prevent endpoint/UI loops.
            if clarification_request:
                plan.human_clarification_response = human_clarification
            plan.human_clarification_request = None
        else:
            if clarification_request:
                plan.human_clarification_response = None
            if parsed.refined_clarification_request:
                plan.human_clarification_request = parsed.refined_clarification_request

        await self._memory_store.update_plan(plan)

        await self._memory_store.add_item(
            AgentMessage(
                data_type="agent_message",
                session_id=session_id,
                user_id=self._user_id,
                plan_id=plan.id,
                content=parsed.assistant_message,
                source=AgentType.PLANNER.value,
                step_id="",
            )
        )

        track_event_if_configured(
            "Planner - Evaluated human clarification and responded",
            {
                "session_id": session_id,
                "user_id": self._user_id,
                "plan_id": plan.id,
                "clarification_resolved": parsed.clarification_resolved,
                "source": AgentType.PLANNER.value,
            },
        )

        return parsed.assistant_message

    async def _create_structured_plan(
        self, input_task: InputTask
    ) -> Tuple[Plan, List[Step]]:
        """Create a structured plan with steps based on the input task.

        Args:
            input_task: The input task from the user

        Returns:
            Tuple containing the created plan and list of steps
        """
        try:
            # Generate the instruction for the LLM

            # Get template variables as a dictionary
            args = self._generate_args(input_task.description)

            run_instructions = self._get_template(
                objective=args["objective"],
                agents_str=args["agents_str"],
                tools_str=args["tools_str"],
            )

            thread = None
            # thread = self.client.agents.create_thread(thread_id=input_task.session_id)
            async_generator = self.invoke(
                instructions_override=run_instructions,
                temperature=0.0,
                max_completion_tokens=10096,
                polling_options=self._get_polling_options(),
                response_format=ResponseFormatJsonSchemaType(
                    json_schema=ResponseFormatJsonSchema(
                        name=PlannerResponsePlan.__name__,
                        description=f"respond with {PlannerResponsePlan.__name__.lower()}",
                        schema=PlannerResponsePlan.model_json_schema(),
                    )
                ),
                thread=thread,
            )

            # Call invoke with proper keyword arguments and JSON response schema
            response_content = ""

            # Collect the response from the async generator
            async for chunk in async_generator:
                if chunk is not None:
                    response_content += str(chunk)

            logging.info(f"Response content length: {len(response_content)}")

            # Check if response is empty or whitespace
            if not response_content or response_content.isspace():
                raise ValueError("Received empty response from Azure AI Agent")

            # Parse the JSON response directly to PlannerResponsePlan
            parsed_result = None

            # Try various parsing approaches in sequence
            try:
                # 1. First attempt: Try to parse the raw response directly
                parsed_result = PlannerResponsePlan.model_validate_json(response_content)
                if parsed_result is None:
                    # If all parsing attempts fail, create a fallback plan from the text content
                    logging.info(
                        "All parsing attempts failed, creating fallback plan from text content"
                    )
                    raise ValueError("Failed to parse JSON response")

            except Exception as parsing_exception:
                logging.exception(f"Error during parsing attempts: {parsing_exception}")
                raise ValueError("Failed to parse JSON response")

            # At this point, we have a valid parsed_result

            # Extract plan details and re-hydrate PII tokens with original values
            # The model received redacted text, so its response may contain tokens like {{EMAIL_1}}
            # We need to replace these with the actual values before storing
            pii_context = get_pii_context(input_task.session_id)

            initial_goal = pii_context.rehydrate(parsed_result.initial_goal)
            steps_data = parsed_result.steps
            summary = pii_context.rehydrate(parsed_result.summary_plan_and_steps)
            human_clarification_request = parsed_result.human_clarification_request
            if human_clarification_request:
                human_clarification_request = pii_context.rehydrate(human_clarification_request)

            # Create the Plan instance
            plan = Plan(
                data_type="plan",
                id=str(uuid.uuid4()),
                session_id=input_task.session_id,
                user_id=self._user_id,
                initial_goal=initial_goal,
                overall_status=PlanStatus.in_progress,
                summary=summary,
                human_clarification_request=human_clarification_request,
            )

            # Store the plan
            await self._memory_store.add_plan(plan)

            # Create steps from the parsed data
            steps = []
            for step_data in steps_data:
                # Re-hydrate the action to replace PII tokens with actual values
                action = pii_context.rehydrate(step_data.action)
                agent_name = step_data.agent

                # Validate agent name
                if agent_name.value not in self._available_agents:
                    logging.warning(
                        f"Invalid agent name: {agent_name}, defaulting to {AgentType.GENERIC.value}"
                    )
                    agent_name = AgentType.GENERIC

                # Create the step
                step = Step(
                    data_type="step",
                    id=str(uuid.uuid4()),
                    plan_id=plan.id,
                    session_id=input_task.session_id,
                    user_id=self._user_id,
                    action=action,
                    agent=agent_name,
                    status=StepStatus.planned,
                    human_approval_status=HumanFeedbackStatus.requested,
                )
                steps.append(step)

            # Validate Planner assignments with LLM-based batch validator.
            # Fail-open behavior is implemented inside ValidatorAgent.
            try:
                validator = ValidatorAgent(self, self._available_agents)
                validation_result = await validator.validate_plan_batch(
                    steps=steps,
                    agent_tools=self._agent_tools_list,
                    session_id=input_task.session_id,
                )
                corrections = ValidatorAgent.apply_corrections(
                    steps=steps, validation_result=validation_result
                )
                track_event_if_configured(
                    "Planner - Validation completed",
                    {
                        "plan_id": plan.id,
                        "total_steps": len(steps),
                        "corrections_applied": corrections,
                        "session_id": input_task.session_id,
                        "user_id": self._user_id,
                    },
                )
            except Exception as validation_exc:
                logging.warning(
                    "Validation pass failed for plan %s. Proceeding with planner assignments. Error: %s",
                    plan.id,
                    validation_exc,
                )

            # Store steps (with validator audit and optional corrections applied).
            for step in steps:
                await self._memory_store.add_step(step)
                try:
                    track_event_if_configured(
                        "Planner - Added planned individual step into the cosmos",
                        {
                            "plan_id": plan.id,
                            "action": step.action,
                            "agent": step.agent,
                            "status": StepStatus.planned,
                            "session_id": input_task.session_id,
                            "user_id": self._user_id,
                            "human_approval_status": HumanFeedbackStatus.requested,
                            "validator_decision": step.validator_decision,
                            "confidence_score": step.confidence_score,
                        },
                    )
                except Exception as event_error:
                    logging.warning(f"Error in event tracking: {event_error}")

            return plan, steps

        except Exception as e:
            error_message = str(e)
            if "Rate limit is exceeded" in error_message:
                logging.warning("Rate limit hit. Consider retrying after some delay.")
                raise
            else:
                logging.exception(f"Error creating structured plan: {e}")

            # Create a fallback dummy plan when parsing fails
            logging.info("Creating fallback dummy plan due to parsing error")

            # Create a dummy plan with the original task description
            dummy_plan = Plan(
                data_type="plan",
                id=str(uuid.uuid4()),
                session_id=input_task.session_id,
                user_id=self._user_id,
                initial_goal=input_task.description,
                overall_status=PlanStatus.in_progress,
                summary=f"Plan created for: {input_task.description}",
                human_clarification_request=None,
            )

            # Store the dummy plan
            await self._memory_store.add_plan(dummy_plan)

            # Create a dummy step for analyzing the task
            dummy_step = Step(
                data_type="step",
                id=str(uuid.uuid4()),
                plan_id=dummy_plan.id,
                session_id=input_task.session_id,
                user_id=self._user_id,
                action="Analyze the task: " + input_task.description,
                agent=AgentType.GENERIC,
                status=StepStatus.planned,
                human_approval_status=HumanFeedbackStatus.requested,
            )

            # Store the dummy step
            await self._memory_store.add_step(dummy_step)

            # Add a second step to request human clarification
            clarification_step = Step(
                data_type="step",
                id=str(uuid.uuid4()),
                plan_id=dummy_plan.id,
                session_id=input_task.session_id,
                user_id=self._user_id,
                action=f"Provide more details about: {input_task.description}",
                agent=AgentType.HUMAN,
                status=StepStatus.planned,
                human_approval_status=HumanFeedbackStatus.requested,
            )

            # Store the clarification step
            await self._memory_store.add_step(clarification_step)

            # Log the event
            try:
                track_event_if_configured(
                    "Planner - Created fallback dummy plan due to parsing error",
                    {
                        "session_id": input_task.session_id,
                        "user_id": self._user_id,
                        "error": str(e),
                        "description": input_task.description,
                        "source": AgentType.PLANNER.value,
                    },
                )
            except Exception as event_error:
                logging.warning(
                    f"Error in event tracking during fallback: {event_error}"
                )

            return dummy_plan, [dummy_step, clarification_step]

    def _generate_args(self, objective: str) -> Dict[str, str]:
        """Generate instruction for the LLM to create a plan.

        Args:
            objective: The user's objective

        Returns:
            Dictionary containing the variables to populate the template
        """
        # Create a list of available agents
        agents_str = ", ".join(self._available_agents)

        # Create list of available tools in JSON-like format
        tools_list = []

        for agent_name, tools in self._agent_tools_list.items():
            if agent_name in self._available_agents:
                tools_list.append(tools)

        tools_str = str(tools_list)

        # Return a dictionary with template variables
        return {
            "objective": objective,
            "agents_str": agents_str,
            "tools_str": tools_str,
        }

    @staticmethod
    def _get_template(
        objective: str = "No objective provided yet.",
        agents_str: str = (
            "Human_Agent, Hr_Agent, Marketing_Agent, Product_Agent, "
            "Procurement_Agent, Tech_Support_Agent, Generic_Agent"
        ),
        tools_str: str = "Use the tools available to each assigned agent.",
    ) -> str:
        """Generate the instruction template for the LLM."""

        instruction_template = f"""
            You are the Planner, an AI orchestrator that manages a group of AI agents to accomplish tasks.

            For the given objective, come up with a simple step-by-step plan.
            This plan should involve individual tasks that, if executed correctly, will yield the correct answer. Do not add any superfluous steps.
            The result of the final step should be the final answer. Make sure that each step has all the information needed - do not skip steps.

            These actions are passed to the specific agent. Make sure the action contains all the information required for the agent to execute the task.

            Your objective is:
            {objective}

            The agents you have access to are:
            {agents_str}

            These agents have access to the following functions:
            {tools_str}

            If additional information is truly required, the first step of your plan should ask the user for that information.

            Only use the functions provided as part of your plan. If the task is not possible with the agents and tools provided, create a step with the agent of type Human and mark the overall status as completed.

            Do not add superfluous steps - only take the most direct path to the solution, with the minimum number of steps. Only do the minimum necessary to complete the goal.

            If there is a single function call that can directly solve the entire objective, you may generate a single-step plan.
            However, if the objective asks for multiple explicit deliverables (for example: generate output, validate/verify, report metadata, provide summary, and define failure handling), do not collapse into one step.
            In that case, create the minimum multi-step plan required so each deliverable is explicitly covered.

            When generating the action in the plan, frame the action as an instruction you are passing to the agent to execute. It should be a short, single sentence. Include the function to use. For example, "Set up an Office 365 Account for Jessica Smith. Function: set_up_office_365_account"

            Ensure the summary of the plan and the overall steps is less than 50 words.

            Identify any additional information that might be required to complete the task. Include this information in the plan in the human_clarification_request field of the plan. If it is not required, leave it as null.
            Do not request credentials through HumanAgent when provider tools already exist. Assign execution to the appropriate specialist agent and allow tools to return structured credentials_required responses if onboarding is needed.

            When identifying required information, consider what input a GenericAgent or fallback LLM model would need to perform the task correctly. This may include:
            - Input data, text, or content to process
            - A question to answer or topic to describe
            - Any referenced material that is mentioned but not actually included (e.g., "the given text")
            - A clear subject or target when the task instruction is too vague (e.g., "describe," "summarize," or "analyze" without specifying what to describe)

            If such required input is missing—even if not explicitly referenced—generate a concise clarification request in the human_clarification_request field.

            Do not include information that you are waiting for clarification on in the string of the action field, as this otherwise won't get updated.

            You must prioritise using the provided functions to accomplish each step. First evaluate each and every function the agents have access too. Only if you cannot find a function needed to complete the task, and you have reviewed each and every function, and determined why each are not suitable, there are two options you can take when generating the plan.
            First evaluate whether the step could be handled by a typical large language model, without any specialised functions. For example, tasks such as "add 32 to 54", or "convert this SQL code to a python script", or "write a 200 word story about a fictional product strategy".
            If a general Large Language Model CAN handle the step/required action, add a step to the plan with the action you believe would be needed. Assign these steps to the GenericAgent. For example, if the task is to convert the following SQL into python code (SELECT * FROM employees;), and there is no function to convert SQL to python, write a step with the action "convert the following SQL into python code (SELECT * FROM employees;)" and assign it to the GenericAgent.
            Alternatively, if a general Large Language Model CAN NOT handle the step/required action, add a step to the plan with the action you believe would be needed and assign it to the HumanAgent. For example, if the task is to find the best way to get from A to B, and there is no function to calculate the best route, write a step with the action "Calculate the best route from A to B." and assign it to the HumanAgent.

            Limit the plan to 6 steps or less.

            Choose from {agents_str} ONLY for planning your steps.

            """
        return instruction_template
