import logging
from abc import abstractmethod
from typing import Any, List, Mapping, Optional, cast

# Import the new AppConfig instance
from app_config import config
from azure.ai.agents.models import FunctionToolDefinition
from azure.ai.agents.models import (
    ResponseFormatJsonSchema,
    ResponseFormatJsonSchemaType,
)
from context.cosmos_memory_kernel import CosmosMemoryContext
from event_utils import track_event_if_configured
from models.messages_kernel import (ActionRequest, ActionResponse,
                                    AgentMessage, StepStatus)
from semantic_kernel.agents.azure_ai.agent_thread_actions import AgentThreadActions
from semantic_kernel.exceptions.agent_exceptions import AgentInvokeException
from semantic_kernel.agents.azure_ai.azure_ai_agent import AzureAIAgent
from semantic_kernel.functions import KernelFunction
from semantic_kernel.functions.kernel_function_metadata import KernelFunctionMetadata

# Default formatting instructions used across agents
DEFAULT_FORMATTING_INSTRUCTIONS = "Instructions: returning the output of this function call verbatim to the user in markdown. Then write AGENT SUMMARY: and then include a summary of what you did."


def _patch_tool_validation_for_prefixed_kernel_names() -> None:
    """Patch SK validation to accept unqualified tool names against qualified kernel names."""
    if getattr(AgentThreadActions, "_macae_tool_validation_patched", False):
        return

    def _normalize_name(name: Optional[str]) -> set[str]:
        if not name:
            return set()
        normalized = {name}
        for sep in ("-", ".", "::"):
            if sep in name:
                normalized.add(name.split(sep)[-1])
        return normalized

    def _validate_function_tools_registered(tools: list[Any], funcs: list[Any]) -> None:
        function_tool_names: set[str] = set()
        for tool in tools:
            if isinstance(tool, FunctionToolDefinition):
                tool_name = getattr(tool.function, "name", None)
                if tool_name:
                    function_tool_names.add(tool_name)

        kernel_function_names: set[str] = set()
        for f in funcs:
            if isinstance(f, KernelFunctionMetadata):
                kernel_function_names |= _normalize_name(f.fully_qualified_name)
                kernel_function_names |= _normalize_name(f.name)
            else:
                kernel_function_names |= _normalize_name(
                    getattr(f, "full_qualified_name", None)
                )
                kernel_function_names |= _normalize_name(getattr(f, "name", None))

        missing = function_tool_names - kernel_function_names
        if missing:
            raise AgentInvokeException(
                "The following function tool(s) are defined on the agent but missing "
                f"from the kernel: {sorted(missing)}. "
                "Please ensure all required tools are registered with the kernel."
            )

    AgentThreadActions._validate_function_tools_registered = staticmethod(  # type: ignore[attr-defined]
        _validate_function_tools_registered
    )
    AgentThreadActions._macae_tool_validation_patched = True  # type: ignore[attr-defined]


_patch_tool_validation_for_prefixed_kernel_names()


class BaseAgent(AzureAIAgent):
    """BaseAgent implemented using Semantic Kernel with Azure AI Agent support."""

    def __init__(
        self,
        agent_name: str,
        session_id: str,
        user_id: str,
        memory_store: CosmosMemoryContext,
        tools: Optional[List[KernelFunction]] = None,
        system_message: Optional[str] = None,
        client=None,
        definition=None,
    ):
        """Initialize the base agent.

        Args:
            agent_name: The name of the agent
            session_id: The session ID
            user_id: The user ID
            memory_store: The memory context for storing agent state
            tools: Optional list of tools for the agent
            system_message: Optional system message for the agent
            agent_type: Optional agent type string for automatic tool loading
            client: The client required by AzureAIAgent
            definition: The definition required by AzureAIAgent
        """

        tools = tools or []
        system_message = system_message or self.default_system_message(agent_name)

        # Call AzureAIAgent constructor with required client and definition
        super().__init__(
            deployment_name=None,  # Set as needed
            plugins=cast(Any, tools),  # SK expects plugin objects; runtime accepts current list.
            endpoint=None,  # Set as needed
            api_version=None,  # Set as needed
            token=None,  # Set as needed
            model=config.AZURE_OPENAI_DEPLOYMENT_NAME,
            agent_name=agent_name,
            system_prompt=system_message,
            client=cast(Any, client),
            definition=cast(Any, definition),
        )

        # Store instance variables
        self._agent_name = agent_name
        self._session_id = session_id
        self._user_id = user_id
        self._memory_store = memory_store
        self._tools = tools
        self._system_message = system_message
        self._chat_history = [{"role": "system", "content": self._system_message}]
        # self._agent = None  # Will be initialized in async_init

        # Required properties for AgentGroupChat compatibility
        self.name = agent_name  # This is crucial for AgentGroupChat to identify agents

        # Register functions in kernel metadata for SK tool validation at invoke-time.
        if tools and getattr(self, "kernel", None) is not None:
            try:
                self.kernel.add_functions(plugin_name=self._agent_name, functions=tools)
            except Exception as exc:
                logging.warning(
                    "Failed to register tools into kernel for %s: %s",
                    self._agent_name,
                    exc,
                )

    # @property
    # def plugins(self) -> Optional[dict[str, Callable]]:
    #     """Get the plugins for this agent.

    #     Returns:
    #         A list of plugins, or None if not applicable.
    #     """
    #     return None
    @staticmethod
    def default_system_message(agent_name=None) -> str:
        name = agent_name
        return f"You are an AI assistant named {name}. Help the user by providing accurate and helpful information."

    async def handle_action_request(self, action_request: ActionRequest) -> str:
        """Handle an action request from another agent or the system.

        Args:
            action_request_json: The action request as a JSON string

        Returns:
            A JSON string containing the action response
        """

        # Get the step from memory
        step = await self._memory_store.get_step(
            action_request.step_id, action_request.session_id
        )

        if not step:
            # Create error response if step not found
            response = ActionResponse(
                step_id=action_request.step_id,
                plan_id=action_request.plan_id,
                session_id=action_request.session_id,
                result="Step not found in memory.",
                status=StepStatus.failed,
            )
            return response.model_dump_json()

        # Add messages to chat history for context
        # This gives the agent visibility of the conversation history
        self._chat_history.extend(
            [
                {"role": "assistant", "content": action_request.action},
                {
                    "role": "user",
                    "content": f"{step.human_feedback}. Now make the function call",
                },
            ]
        )

        try:
            # Use the agent to process the action
            # chat_history = self._chat_history.copy()

            # Call the agent to handle the action
            thread = None
            # thread = self.client.agents.get_thread(
            #     thread=step.session_id
            # )  # AzureAIAgentThread(thread_id=step.session_id)
            async_generator = self.invoke(
                messages=f"{str(self._chat_history)}\n\nPlease perform this action : {step.action}",
                thread=thread,
            )

            response_content = ""

            # Collect the response from the async generator
            async for chunk in async_generator:
                if chunk is not None:
                    response_content += str(chunk)

            logging.info(f"Response content length: {len(response_content)}")
            logging.info(f"Response content: {response_content}")

            # Store agent message in cosmos memory
            await self._memory_store.add_item(
                AgentMessage(
                    data_type="agent_message",
                    session_id=action_request.session_id,
                    user_id=self._user_id,
                    plan_id=action_request.plan_id,
                    content=f"{response_content}",
                    source=self._agent_name,
                    step_id=action_request.step_id,
                )
            )

            # Track telemetry
            track_event_if_configured(
                "Base agent - Added into the cosmos",
                {
                    "session_id": action_request.session_id,
                    "user_id": self._user_id,
                    "plan_id": action_request.plan_id,
                    "content": f"{response_content}",
                    "source": self._agent_name,
                    "step_id": action_request.step_id,
                },
            )

        except Exception as e:
            logging.exception(f"Error during agent execution: {e}")

            # Track error in telemetry
            track_event_if_configured(
                "Base agent - Error during agent execution, captured into the cosmos",
                {
                    "session_id": action_request.session_id,
                    "user_id": self._user_id,
                    "plan_id": action_request.plan_id,
                    "content": f"{e}",
                    "source": self._agent_name,
                    "step_id": action_request.step_id,
                },
            )

            # Return an error response
            response = ActionResponse(
                step_id=action_request.step_id,
                plan_id=action_request.plan_id,
                session_id=action_request.session_id,
                result=f"Error: {str(e)}",
                status=StepStatus.failed,
            )
            return response.model_dump_json()

        # Update step status
        step.status = StepStatus.completed
        step.agent_reply = response_content
        await self._memory_store.update_step(step)

        # Track step completion in telemetry
        track_event_if_configured(
            "Base agent - Updated step and updated into the cosmos",
            {
                "status": StepStatus.completed,
                "session_id": action_request.session_id,
                "agent_reply": f"{response_content}",
                "user_id": self._user_id,
                "plan_id": action_request.plan_id,
                "content": f"{response_content}",
                "source": self._agent_name,
                "step_id": action_request.step_id,
            },
        )

        # Create and return action response
        response = ActionResponse(
            step_id=step.id,
            plan_id=step.plan_id,
            session_id=action_request.session_id,
            result=response_content,
            status=StepStatus.completed,
        )

        return response.model_dump_json()

    def save_state(self) -> Mapping[str, Any]:
        """Save the state of this agent."""
        # CosmosMemoryContext does not implement save_state/load_state.
        return {"session_id": self._session_id, "user_id": self._user_id}

    def load_state(self, state: Mapping[str, Any]) -> None:
        """Load the state of this agent."""
        _ = state

    @classmethod
    @abstractmethod
    async def create(cls, **kwargs) -> "BaseAgent":
        """Create an instance of the agent."""
        pass

    @staticmethod
    async def _create_azure_ai_agent_definition(
        agent_name: str,
        instructions: str,
        tools: Optional[List[KernelFunction]] = None,
        client=None,
        response_format: Optional[Any] = None,
        temperature: float = 0.0,
    ):
        """
        Creates a new Azure AI Agent with the specified name and instructions using AIProjectClient.
        If an agent with the given name (assistant_id) already exists, it tries to retrieve it first.

        Args:
            kernel: The Semantic Kernel instance
            agent_name: The name of the agent (will be used as assistant_id)
            instructions: The system message / instructions for the agent
            agent_type: The type of agent (defaults to "assistant")
            tools: Optional tool definitions for the agent
            tool_resources: Optional tool resources required by the tools
            response_format: Optional response format to control structured output
            temperature: The temperature setting for the agent (defaults to 0.0)

        Returns:
            A new AzureAIAgent definition or an existing one if found
        """
        try:
            # Get the AIProjectClient
            if client is None:
                client = config.get_ai_project_client()
            client = cast(Any, client)

            # Validate and convert response_format if it's a dict
            if response_format is not None and isinstance(response_format, dict):
                logging.info(
                    "Converting dict response_format to Azure Agents models type."
                )
                try:
                    # Extract data from the dict sent by Semantic Kernel
                    js_data = response_format.get("json_schema", {})
                    
                    # Re-package as strong objects from Azure SDK
                    response_format = ResponseFormatJsonSchemaType(
                        json_schema=ResponseFormatJsonSchema(
                            name=js_data.get("name", "PlannerSchema"),
                            description=js_data.get("description", "Structured response"),
                            schema=js_data.get("schema", {})
                        )
                    )
                except Exception as e:
                    logging.warning(f"Failed to convert response_format: {e}. Keeping original.")

            # # First try to get an existing agent with this name as assistant_id
            try:
                agent_id = None
                agent_list = client.agents.list_agents()
                async for agent in agent_list:
                    if agent.name == agent_name:
                        agent_id = agent.id
                        break
                # If the agent already exists, we can use it directly
                # Get the existing agent definition
                if agent_id is not None:
                    logging.info(f"Agent with ID {agent_id} exists.")
                    existing_definition = await client.agents.get_agent(agent_id)
                    existing_response_format = getattr(
                        existing_definition, "response_format", None
                    )

                    # Telemetry workaround:
                    # if an existing agent stores response_format incorrectly (not ResponseFormatJsonSchemaType)
                    # and we need structured output, reuse will fail at run creation.
                    if response_format is not None:
                        if not isinstance(existing_response_format, ResponseFormatJsonSchemaType):
                            logging.info(
                                "Agent %s (ID: %s) has incompatible response_format type %s; deleting and recreating.",
                                agent_name,
                                agent_id,
                                type(existing_response_format).__name__,
                            )
                            await client.agents.delete_agent(agent_id)
                        else:
                            return existing_definition
                    else:
                        return existing_definition
            except Exception as e:
                # The Azure AI Projects SDK throws an exception when the agent doesn't exist
                # (not returning None), so we catch it and proceed to create a new agent
                if "ResourceNotFound" in str(e) or "404" in str(e):
                    logging.info(
                        f"Agent with ID {agent_name} not found. Will create a new one."
                    )
                else:
                    # Log unexpected errors but still try to create a new agent
                    logging.warning(
                        f"Unexpected error while retrieving agent {agent_name}: {str(e)}. Attempting to create new agent."
                    )

            # Create the agent using the project client with the agent_name as both name and assistantId
            agent_definition = await client.agents.create_agent(
                model=config.AZURE_OPENAI_DEPLOYMENT_NAME,
                name=agent_name,
                instructions=instructions,
                temperature=temperature,
                response_format=cast(Any, response_format),
            )

            return agent_definition
        except Exception as exc:
            logging.error("Failed to create Azure AI Agent: %s", exc)
            raise
