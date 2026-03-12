import logging
from typing import Any, List, Optional, cast

from context.cosmos_memory_kernel import CosmosMemoryContext
from kernel_agents.agent_base import BaseAgent
from kernel_tools.marketing_tools import MarketingTools
from models.messages_kernel import AgentType
from semantic_kernel.functions import KernelFunction


class MarketingAgent(BaseAgent):
    """Marketing agent implementation using Semantic Kernel.

    This agent specializes in marketing, campaign management, and analyzing market data.
    """

    def __init__(
        self,
        session_id: str,
        user_id: str,
        memory_store: CosmosMemoryContext,
        tools: Optional[List[KernelFunction]] = None,
        system_message: Optional[str] = None,
        agent_name: str = AgentType.MARKETING.value,
        client=None,
        definition=None,
    ) -> None:
        """Initialize the Marketing Agent.

        Args:
            kernel: The semantic kernel instance
            session_id: The current session identifier
            user_id: The user identifier
            memory_store: The Cosmos memory context
            tools: List of tools available to this agent (optional)
            system_message: Optional system message for the agent
            agent_name: Optional name for the agent (defaults to "MarketingAgent")
            client: Optional client instance
            definition: Optional definition instance
        """
        # Load configuration if tools not provided
        if not tools:
            # Get tools directly from MarketingTools class
            tools_dict = MarketingTools.get_all_kernel_functions()
            tools = [KernelFunction.from_method(func) for func in tools_dict.values()]

        # Use system message from config if not explicitly provided
        if not system_message:
            system_message = self.default_system_message(agent_name)

        # Use agent name from config if available
        agent_name = AgentType.MARKETING.value

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

    @classmethod
    async def create(
        cls,
        **kwargs: Any,
    ) -> "MarketingAgent":
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
        agent_name = cast(Optional[str], kwargs.get("agent_name"))
        # Ensure agent_name is a string, fallback to AgentType.MARKETING.value if not
        if not isinstance(agent_name, str) or not agent_name:
            agent_name = AgentType.MARKETING.value
        client = kwargs.get("client")
        if not session_id or not user_id or memory_store is None:
            raise ValueError("session_id, user_id, and memory_store are required")
        if not system_message:
            system_message = cls.default_system_message(agent_name)

        # Load tools if not provided - MUST happen before _create_azure_ai_agent_definition
        if not tools:
            tools_dict = MarketingTools.get_all_kernel_functions()
            # Ensure all tools are KernelFunction type
            tools = [
                cast(KernelFunction, KernelFunction.from_method(func))
                for func in tools_dict.values()
            ]
            logging.info(f"Loaded {len(tools)} tools for MarketingAgent")

        try:
            logging.info("Initializing MarketingAgent from async init azure AI Agent")

            # Create the Azure AI Agent using AppConfig with string instructions
            agent_definition = await cls._create_azure_ai_agent_definition(
                agent_name=agent_name,
                instructions=system_message,
                tools=cast(List[KernelFunction], tools),  # Ensure correct type
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
        return "You are a Marketing agent. You specialize in marketing strategy, campaign development, content creation, and market analysis. You help create effective marketing campaigns, analyze market data, and develop promotional content for products and services."

    @property
    def plugins(self):
        """Get the plugins for the marketing agent."""
        return MarketingTools.get_all_kernel_functions()
