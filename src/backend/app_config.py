# app_config.py
import logging
import os
from typing import Any, Optional

# Disable Azure AI Agents OTEL preview telemetry path that fails on dict response_format.
# Must be set before importing Azure SDK modules.
os.environ.setdefault("AZURE_TRACING_GEN_AI_ENABLE_OTEL", "false")

from azure.ai.projects.aio import AIProjectClient
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv
from semantic_kernel.kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion

# Load environment variables from .env file
load_dotenv()


class AppConfig:
    """Application configuration class that loads settings from environment variables."""

    def __init__(self):
        """Initialize the application configuration with environment variables."""
        # Azure authentication settings
        self.AZURE_TENANT_ID = self._get_optional("AZURE_TENANT_ID")
        self.AZURE_CLIENT_ID = self._get_optional("AZURE_CLIENT_ID")
        self.AZURE_CLIENT_SECRET = self._get_optional("AZURE_CLIENT_SECRET")

        # CosmosDB settings
        self.COSMOSDB_ENDPOINT = self._get_optional("COSMOSDB_ENDPOINT")
        self.COSMOSDB_DATABASE = self._get_optional("COSMOSDB_DATABASE")
        self.COSMOSDB_CONTAINER = self._get_optional("COSMOSDB_CONTAINER")
        self.COSMOS_CONNECTION_STRING = self._get_optional("COSMOS_CONNECTION_STRING")
        self.COSMOS_ACCOUNT_KEY = self._get_optional("COSMOS_ACCOUNT_KEY")

        # Azure OpenAI settings
        self.AZURE_OPENAI_DEPLOYMENT_NAME = self._get_required(
            "AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o"
        )
        self.AZURE_OPENAI_API_VERSION = self._get_required(
            "AZURE_OPENAI_API_VERSION", "2024-11-20"
        )
        self.AZURE_OPENAI_ENDPOINT = self._get_required("AZURE_OPENAI_ENDPOINT")
        self.AZURE_OPENAI_SCOPES = [
            f"{self._get_optional('AZURE_OPENAI_SCOPE', 'https://cognitiveservices.azure.com/.default')}"
        ]

        # Frontend settings
        self.FRONTEND_SITE_NAME = self._get_optional(
            "FRONTEND_SITE_NAME", "http://127.0.0.1:3000"
        )

        # Azure AI settings
        self.AZURE_AI_SUBSCRIPTION_ID = self._get_required("AZURE_AI_SUBSCRIPTION_ID")
        self.AZURE_AI_RESOURCE_GROUP = self._get_required("AZURE_AI_RESOURCE_GROUP")
        self.AZURE_AI_PROJECT_NAME = self._get_required("AZURE_AI_PROJECT_NAME")
        self.AZURE_AI_AGENT_ENDPOINT = self._get_required("AZURE_AI_AGENT_ENDPOINT")
        self.AZURE_AI_PROJECT_ENDPOINT = self._get_optional("AZURE_AI_PROJECT_ENDPOINT")

        # Cached clients and resources
        self._azure_credentials = None
        self._cosmos_client = None
        self._cosmos_database = None
        self._ai_project_client = None

    def _get_required(self, name: str, default: Optional[str] = None) -> str:
        """Get a required configuration value from environment variables.

        Args:
            name: The name of the environment variable
            default: Optional default value if not found

        Returns:
            The value of the environment variable or default if provided

        Raises:
            ValueError: If the environment variable is not found and no default is provided
        """
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            logging.warning(
                "Environment variable %s not found, using default value", name
            )
            return default
        raise ValueError(
            f"Environment variable {name} not found and no default provided"
        )

    def _get_optional(self, name: str, default: str = "") -> str:
        """Get an optional configuration value from environment variables.

        Args:
            name: The name of the environment variable
            default: Default value if not found (default: "")

        Returns:
            The value of the environment variable or the default value
        """
        if name in os.environ:
            return os.environ[name]
        return default

    def _get_bool(self, name: str) -> bool:
        """Get a boolean configuration value from environment variables.

        Args:
            name: The name of the environment variable

        Returns:
            True if the environment variable exists and is set to 'true' or '1', False otherwise
        """
        return name in os.environ and os.environ[name].lower() in ["true", "1"]

    def get_azure_credentials(self) -> DefaultAzureCredential:
        """Get Azure credentials using DefaultAzureCredential.

        Returns:
            DefaultAzureCredential instance for Azure authentication
        """
        # Cache the credentials object
        if self._azure_credentials is not None:
            return self._azure_credentials

        try:
            self._azure_credentials = DefaultAzureCredential()
            return self._azure_credentials
        except Exception as exc:
            logging.error("Failed to create DefaultAzureCredential: %s", exc)
            raise RuntimeError("Unable to create DefaultAzureCredential") from exc

    def get_cosmos_database_client(self):
        """Get a Cosmos DB client for the configured database.

        Returns:
            A Cosmos DB database client
        """
        try:
            if self._cosmos_client is None:
                # Use connection string if available, otherwise use account key or AAD
                if self.COSMOS_CONNECTION_STRING:
                    self._cosmos_client = CosmosClient.from_connection_string(
                        self.COSMOS_CONNECTION_STRING
                    )
                elif self.COSMOS_ACCOUNT_KEY:
                    self._cosmos_client = CosmosClient(
                        self.COSMOSDB_ENDPOINT, credential=self.COSMOS_ACCOUNT_KEY
                    )
                else:
                    credential = self.get_azure_credentials()
                    if credential is None:
                        raise RuntimeError("No Cosmos DB credentials available")
                    self._cosmos_client = CosmosClient(
                        self.COSMOSDB_ENDPOINT, credential=credential
                    )

            if self._cosmos_database is None:
                self._cosmos_database = self._cosmos_client.get_database_client(
                    self.COSMOSDB_DATABASE
                )

            return self._cosmos_database
        except Exception as exc:
            logging.error(
                "Failed to create CosmosDB client: %s. CosmosDB is required for this application.",
                exc,
            )
            raise

    def create_kernel(self):
        """Creates a new Semantic Kernel instance with Azure OpenAI chat service.

        Returns:
            A new Semantic Kernel instance configured with AzureChatCompletion
        """
        kernel = Kernel()

        # Add the Azure OpenAI chat completion service
        # This is required for ChatCompletionAgent to work
        chat_service = AzureChatCompletion(
            deployment_name=self.AZURE_OPENAI_DEPLOYMENT_NAME,
            endpoint=self.AZURE_OPENAI_ENDPOINT,
            api_version=self.AZURE_OPENAI_API_VERSION,
            ad_token_provider=self._get_token_provider(),
            service_id="agent_chat_service",  # Named service for agents
        )
        kernel.add_service(chat_service)

        logging.info(f"Kernel created with AzureChatCompletion service: {self.AZURE_OPENAI_DEPLOYMENT_NAME}")
        return kernel

    def _get_token_provider(self) -> Any:
        """Get an async token provider for Azure OpenAI authentication.

        Returns:
            A callable that returns access tokens for Azure OpenAI
        """
        from azure.identity.aio import get_bearer_token_provider
        credential = self.get_azure_credentials()
        return get_bearer_token_provider(
            credential,
            "https://cognitiveservices.azure.com/.default",
        )

    def get_ai_project_client(self):
        """Create and return an AIProjectClient for Azure AI Foundry using from_connection_string.

        Returns:
            An AIProjectClient instance
        """
        if self._ai_project_client is not None:
            # If the cached client was closed earlier, discard it and recreate.
            try:
                transport = getattr(self._ai_project_client, "_client", None)
                transport = getattr(transport, "_pipeline", None)
                transport = getattr(transport, "_transport", None)
                session_owner = getattr(transport, "_session_owner", None)
                if session_owner is not None and getattr(session_owner, "closed", False):
                    logging.info("Cached AIProjectClient transport is closed. Recreating client.")
                    self._ai_project_client = None
                else:
                    return self._ai_project_client
            except Exception:
                # If internals are not accessible, fall back to reusing cached client.
                return self._ai_project_client

        try:
            credential = self.get_azure_credentials()
            if credential is None:
                raise RuntimeError(
                    "Unable to acquire Azure credentials; ensure DefaultAzureCredential is configured"
                )

            endpoint = self.AZURE_AI_PROJECT_ENDPOINT or self.AZURE_AI_AGENT_ENDPOINT
            endpoint = endpoint.strip().strip('"').strip("'")
            if endpoint.endswith("/"):
                endpoint = endpoint[:-1]

            # Normalize bare Foundry project endpoint to the API path expected by SDK.
            if (
                ".services.ai.azure.com" in endpoint
                and "/api/projects/" not in endpoint
                and self.AZURE_AI_PROJECT_NAME
            ):
                endpoint = f"{endpoint}/api/projects/{self.AZURE_AI_PROJECT_NAME}"

            # Create client with increased timeout for agent operations
            from azure.core.pipeline.transport import AioHttpTransport
            transport = AioHttpTransport(connection_timeout=300, read_timeout=300)

            self._ai_project_client = AIProjectClient(
                endpoint=endpoint,
                credential=credential,
                transport=transport
            )

            return self._ai_project_client
        except Exception as exc:
            logging.error("Failed to create AIProjectClient: %s", exc)
            raise

    async def close(self) -> None:
        """Close all cached async clients properly to prevent resource leaks.

        This method should be called during application shutdown to ensure
        all aiohttp sessions and TCP connections are properly closed.
        """
        errors = []

        # Close AI Project Client
        if self._ai_project_client is not None:
            try:
                await self._ai_project_client.close()
                logging.info("AIProjectClient closed successfully")
            except Exception as exc:
                errors.append(f"AIProjectClient: {exc}")
            finally:
                self._ai_project_client = None

        # Close Cosmos Client
        if self._cosmos_client is not None:
            try:
                await self._cosmos_client.close()
                logging.info("CosmosClient closed successfully")
            except Exception as exc:
                errors.append(f"CosmosClient: {exc}")
            finally:
                self._cosmos_client = None
                self._cosmos_database = None

        # Close Azure Credentials
        if self._azure_credentials is not None:
            try:
                await self._azure_credentials.close()
                logging.info("DefaultAzureCredential closed successfully")
            except Exception as exc:
                errors.append(f"DefaultAzureCredential: {exc}")
            finally:
                self._azure_credentials = None

        if errors:
            logging.warning(f"Errors during client cleanup: {', '.join(errors)}")
        else:
            logging.info("All async clients closed successfully")


# Create a global instance of AppConfig
config = AppConfig()
