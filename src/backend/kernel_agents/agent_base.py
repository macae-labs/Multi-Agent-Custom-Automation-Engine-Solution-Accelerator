import logging
import os
import re
from datetime import timedelta
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
from semantic_kernel.agents.open_ai.run_polling_options import RunPollingOptions
from semantic_kernel.connectors.ai.function_calling_utils import (
    kernel_function_metadata_to_function_call_format,
)
from semantic_kernel.exceptions.agent_exceptions import AgentInvokeException
from semantic_kernel.agents.azure_ai.azure_ai_agent import AzureAIAgent
from semantic_kernel.functions import KernelFunction
from semantic_kernel.functions.kernel_function_metadata import KernelFunctionMetadata

# Default formatting instructions used across agents
DEFAULT_FORMATTING_INSTRUCTIONS = "Instructions: returning the output of this function call verbatim to the user in markdown. Then write AGENT SUMMARY: and then include a summary of what you did."
MAX_AZURE_AGENT_TOOLS = 128
MAX_AZURE_AGENT_TOOLS_PER_RUN = 64


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

    def _validate_function_tools_registered(tools: list[Any], funcs: list[Any]) -> tuple[list[Any], set[str]]:
        function_tool_names: set[str] = set()
        valid_tools: list[Any] = []
        for tool in tools:
            if isinstance(tool, FunctionToolDefinition):
                tool_name = getattr(tool.function, "name", None)
                if tool_name:
                    function_tool_names.add(tool_name)
                    valid_tools.append(tool)

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
            logging.warning(
                "The following function tool(s) are defined on the agent but missing from the kernel: %s. "
                "Continuing without these tools for this run.",
                sorted(missing),
            )
            valid_tools = [
                tool
                for tool in valid_tools
                if getattr(getattr(tool, "function", None), "name", None) not in missing
            ]
        return valid_tools, missing

    def _get_tools_with_cap(cls: Any, agent: "AzureAIAgent", kernel: Any) -> list[Any]:
        """Use agent-scoped tools only and enforce Azure max tool count."""
        tools: list[Any] = list(agent.definition.tools)
        allowed_tool_names = getattr(agent, "_macae_allowed_tool_names", None)
        if allowed_tool_names:
            filtered_tools: list[Any] = []
            for tool in tools:
                function_obj = getattr(tool, "function", None)
                tool_name = getattr(function_obj, "name", None)
                if tool_name and tool_name in allowed_tool_names:
                    filtered_tools.append(tool)
            if filtered_tools:
                tools = filtered_tools

        funcs = kernel.get_full_list_of_function_metadata()
        tools, _missing = cls._validate_function_tools_registered(tools, funcs)
        tools = [cls._prepare_tool_definition(tool) for tool in tools]
        if len(tools) > MAX_AZURE_AGENT_TOOLS_PER_RUN:
            logging.warning(
                "Run tools payload exceeds per-run cap (%s > %s); truncating for stability.",
                len(tools),
                MAX_AZURE_AGENT_TOOLS_PER_RUN,
            )
            tools = tools[:MAX_AZURE_AGENT_TOOLS_PER_RUN]
        if len(tools) > MAX_AZURE_AGENT_TOOLS:
            logging.warning(
                "Run tools payload exceeds Azure limit (%s > %s); truncating to avoid run failure.",
                len(tools),
                MAX_AZURE_AGENT_TOOLS,
            )
            tools = tools[:MAX_AZURE_AGENT_TOOLS]
        return tools

    AgentThreadActions._validate_function_tools_registered = staticmethod(  # type: ignore[attr-defined]
        _validate_function_tools_registered
    )
    AgentThreadActions._get_tools = classmethod(_get_tools_with_cap)  # type: ignore[attr-defined]
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

    def _get_polling_options(self) -> RunPollingOptions:
        """Build polling options for Azure AI Agent runs.

        Timeout is configurable to avoid premature failures on slow runs.
        """
        timeout_seconds = int(os.getenv("AGENT_RUN_POLLING_TIMEOUT_SECONDS", "180"))
        return RunPollingOptions(run_polling_timeout=timedelta(seconds=timeout_seconds))

    @staticmethod
    def _extract_function_name_from_action(action: str) -> Optional[str]:
        match = re.search(r"Function:\s*([a-zA-Z0-9_]+)", action or "")
        return match.group(1) if match else None

    @staticmethod
    def _extract_tool_names_from_local_tools(tools: Optional[List[KernelFunction]]) -> set[str]:
        names: set[str] = set()
        for tool in tools or []:
            metadata = getattr(tool, "metadata", None)
            tool_name = (
                getattr(metadata, "name", None)
                or getattr(tool, "name", None)
                or getattr(tool, "function_name", None)
            )
            if tool_name:
                names.add(str(tool_name))
        return names

    @classmethod
    def _extract_function_mentions_from_text(
        cls, text: str, known_functions: set[str]
    ) -> list[str]:
        candidates: list[str] = []
        explicit = cls._extract_function_name_from_action(text)
        if explicit:
            candidates.append(explicit)
        for token in re.findall(r"\b[a-z][a-z0-9_]{2,}\b", text or ""):
            if token in known_functions:
                candidates.append(token)

        deduped: list[str] = []
        seen = set()
        for name in candidates:
            if name not in seen:
                deduped.append(name)
                seen.add(name)
        return deduped

    def _definition_has_function(self, function_name: str) -> bool:
        definition_tools = getattr(self.definition, "tools", None) or []
        names = self._extract_function_tool_names(cast(list[Any], definition_tools))
        return function_name in names

    async def _refresh_agent_definition(self, reason: str) -> None:
        """Force-refresh remote agent definition and keep local instance in sync."""
        client = self.client or config.get_ai_project_client()
        existing_id = getattr(self.definition, "id", None)
        if existing_id:
            logging.warning(
                "Refreshing Azure agent definition for %s (id=%s). Reason: %s",
                self._agent_name,
                existing_id,
                reason,
            )
            try:
                await client.agents.delete_agent(existing_id)
            except Exception as exc:
                logging.warning(
                    "Failed to delete existing agent definition %s before refresh: %s",
                    existing_id,
                    exc,
                )

        refreshed_definition = await self._create_azure_ai_agent_definition(
            agent_name=self._agent_name,
            instructions=self._system_message,
            tools=self._tools,
            client=client,
            response_format=getattr(self.definition, "response_format", None),
            temperature=0.0,
        )
        self.definition = refreshed_definition
        refreshed_id = getattr(refreshed_definition, "id", None)
        if refreshed_id:
            self.id = refreshed_id
        refreshed_name = getattr(refreshed_definition, "name", None)
        if refreshed_name:
            self.name = refreshed_name

    async def _log_incomplete_run_details(self, exception_message: str) -> None:
        """Best-effort logging of Azure run details for incomplete runs."""
        run_match = re.search(r"run id: `([^`]+)`", exception_message)
        thread_match = re.search(r"thread `([^`]+)`", exception_message)
        if not run_match or not thread_match:
            return

        run_id = run_match.group(1)
        thread_id = thread_match.group(1)
        try:
            run = await self.client.agents.runs.get(run_id=run_id, thread_id=thread_id)
            logging.error(
                "Incomplete run diagnostics for %s: status=%s, last_error=%s, incomplete_details=%s, required_action=%s",
                self._agent_name,
                getattr(run, "status", None),
                getattr(run, "last_error", None),
                getattr(run, "incomplete_details", None),
                getattr(run, "required_action", None),
            )
        except Exception as exc:
            logging.warning(
                "Could not fetch run diagnostics for run %s / thread %s: %s",
                run_id,
                thread_id,
                exc,
            )

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

        # Build a per-invocation context window (system prompt + last 10 turns max)
        # to avoid unbounded growth of the shared chat history across cached agent reuse.
        MAX_HISTORY_TURNS = 10
        history_tail = self._chat_history[:1] + self._chat_history[-(MAX_HISTORY_TURNS * 2):]
        history_tail.extend(
            [
                {"role": "assistant", "content": action_request.action},
                {
                    "role": "user",
                    "content": f"{step.human_feedback}. Now make the function call",
                },
            ]
        )

        local_tool_names = self._extract_tool_names_from_local_tools(self._tools)
        mentioned_functions = self._extract_function_mentions_from_text(
            step.action, local_tool_names
        )
        logging.info(
            "Tool resolution for %s: local_tools=%s, mentioned_functions=%s",
            self._agent_name,
            sorted(local_tool_names),
            mentioned_functions,
        )
        missing_remote_functions = [
            fn for fn in mentioned_functions if not self._definition_has_function(fn)
        ]
        if missing_remote_functions:
            await self._refresh_agent_definition(
                reason=(
                    "missing function(s) in remote definition: "
                    + ", ".join(missing_remote_functions)
                )
            )
            still_missing = [
                fn for fn in missing_remote_functions if not self._definition_has_function(fn)
            ]
            if still_missing:
                logging.error(
                    "Remote definition for %s still missing function(s) after refresh: %s",
                    self._agent_name,
                    ", ".join(still_missing),
                )

        try:
            response_content = ""
            run_tool_filter = set(mentioned_functions) if mentioned_functions else None
            self._macae_allowed_tool_names = run_tool_filter
            for attempt in range(2):
                try:
                    thread = None
                    available_tools_text = (
                        ", ".join(sorted(local_tool_names)) if local_tool_names else "none"
                    )
                    required_fn_text = (
                        ", ".join(mentioned_functions) if mentioned_functions else ""
                    )
                    tool_instruction = (
                        f"Available tools for this agent: {available_tools_text}.\n"
                        + (
                            f"If relevant to the requested step, you MUST call one of these function(s): {required_fn_text}.\n"
                            if required_fn_text
                            else ""
                        )
                        + "Do not claim a function is unavailable if it is listed above."
                    )
                    async_generator = self.invoke(
                        messages=(
                            f"{str(history_tail)}\n\n"
                            f"{tool_instruction}\n\n"
                            f"Please perform this action: {step.action}"
                        ),
                        thread=thread,
                        polling_options=self._get_polling_options(),
                    )

                    # Collect the response from the async generator
                    response_content = ""
                    async for chunk in async_generator:
                        if chunk is not None:
                            response_content += str(chunk)
                    break
                except AgentInvokeException as invoke_exc:
                    error_text = str(invoke_exc)
                    if "status: `incomplete`" in error_text:
                        await self._log_incomplete_run_details(error_text)
                        if attempt == 0:
                            await self._refresh_agent_definition(
                                reason="run status incomplete; retrying once with fresh definition"
                            )
                            continue
                    raise

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
        finally:
            self._macae_allowed_tool_names = None

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
    def _build_function_tool_definitions(
        tools: Optional[List[KernelFunction]],
    ) -> list[dict[str, Any]]:
        """Build Azure function tool definitions from local kernel functions.

        Returns JSON-compatible dict definitions and enforces Azure max tool count.
        """
        if not tools:
            return []

        definitions: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        for kernel_function in tools:
            metadata = getattr(kernel_function, "metadata", None)
            if metadata is None:
                continue
            tool_def = cast(
                dict[str, Any],
                kernel_function_metadata_to_function_call_format(metadata),
            )
            function_name = tool_def.get("function", {}).get("name")
            if not function_name or function_name in seen_names:
                continue
            seen_names.add(function_name)
            definitions.append(tool_def)

        if len(definitions) > MAX_AZURE_AGENT_TOOLS:
            logging.warning(
                "Trimming tool definitions from %s to %s to satisfy Azure limit.",
                len(definitions),
                MAX_AZURE_AGENT_TOOLS,
            )
            definitions = definitions[:MAX_AZURE_AGENT_TOOLS]

        return definitions

    @staticmethod
    def _extract_function_tool_names(tools: list[Any]) -> set[str]:
        """Extract function tool names from Azure tool definitions."""
        names: set[str] = set()
        for tool in tools:
            if isinstance(tool, dict):
                name = tool.get("function", {}).get("name")
                if name:
                    names.add(name)
                continue
            function_obj = getattr(tool, "function", None)
            name = getattr(function_obj, "name", None)
            if name:
                names.add(name)
        return names

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
            desired_tool_defs = BaseAgent._build_function_tool_definitions(tools)
            desired_tool_names = BaseAgent._extract_function_tool_names(desired_tool_defs)

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
                    existing_instructions = getattr(existing_definition, "instructions", "") or ""
                    existing_response_format = getattr(
                        existing_definition, "response_format", None
                    )
                    existing_tools = getattr(existing_definition, "tools", None) or []

                    # Migrate old planner/system instructions that still include
                    # unresolved SK template vars ({{$...}}), which cause runtime warnings.
                    if "{{$" in existing_instructions:
                        logging.info(
                            "Agent %s (ID: %s) has unresolved template variables in instructions; deleting and recreating.",
                            agent_name,
                            agent_id,
                        )
                        await client.agents.delete_agent(agent_id)
                        existing_definition = None

                    # Azure AI Agents currently enforces a maximum tools array length.
                    # If a persisted assistant carries too many tools, every run will fail
                    # with `array_above_max_length`. Recreate once to recover.
                    if existing_definition is not None and len(existing_tools) > 128:
                        logging.info(
                            "Agent %s (ID: %s) has %s tools configured (>128); deleting and recreating.",
                            agent_name,
                            agent_id,
                            len(existing_tools),
                        )
                        await client.agents.delete_agent(agent_id)
                        existing_definition = None

                    # Recreate if persisted definition is desynchronized from local toolset.
                    if existing_definition is not None and desired_tool_names:
                        existing_tool_names = BaseAgent._extract_function_tool_names(
                            existing_tools
                        )
                        if not desired_tool_names.issubset(existing_tool_names):
                            logging.info(
                                "Agent %s (ID: %s) tools are out of sync (existing=%s, desired=%s); deleting and recreating.",
                                agent_name,
                                agent_id,
                                len(existing_tool_names),
                                len(desired_tool_names),
                            )
                            await client.agents.delete_agent(agent_id)
                            existing_definition = None

                    # Telemetry workaround:
                    # if an existing agent stores response_format incorrectly (not ResponseFormatJsonSchemaType)
                    # and we need structured output, reuse will fail at run creation.
                    if existing_definition is not None and response_format is not None:
                        # Azure SDK can return response_format as a plain dict on retrieval.
                        # Treat both structured object and dict as compatible to avoid
                        # deleting/recreating the same agent on every startup.
                        if not isinstance(
                            existing_response_format,
                            (ResponseFormatJsonSchemaType, dict),
                        ):
                            logging.info(
                                "Agent %s (ID: %s) has incompatible response_format type %s; deleting and recreating.",
                                agent_name,
                                agent_id,
                                type(existing_response_format).__name__,
                            )
                            await client.agents.delete_agent(agent_id)
                        else:
                            return existing_definition
                    elif existing_definition is not None:
                        return existing_definition
            except Exception as e:
                # Listing agents can fail for reasons unrelated to the specific agent name
                # (for example: invalid endpoint/project/model access). Keep log generic.
                logging.warning(
                    "Unable to list/retrieve existing agent '%s'. Attempting to create a new one. Error: %s",
                    agent_name,
                    str(e),
                )

            # Create the agent using the project client with the agent_name as both name and assistantId
            agent_definition = await client.agents.create_agent(
                model=config.AZURE_OPENAI_DEPLOYMENT_NAME,
                name=agent_name,
                instructions=instructions,
                tools=cast(Any, desired_tool_defs) if desired_tool_defs else None,
                temperature=temperature,
                response_format=cast(Any, response_format),
            )

            return agent_definition
        except Exception as exc:
            logging.error("Failed to create Azure AI Agent: %s", exc)
            raise
