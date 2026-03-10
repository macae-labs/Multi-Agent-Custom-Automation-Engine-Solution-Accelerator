"""Tool registry and credential management system."""
from enum import Enum
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field


class CredentialRequirement(BaseModel):
    """Structured response when credentials are needed."""
    error_type: str = "credentials_required"
    provider_id: str
    required_fields: List[Dict[str, Any]]
    onboarding_url: str


class ConnectToolResponse(BaseModel):
    """Response after connecting a tool."""
    success: bool
    secret_uri: str
    message: str


class ConnectToolRequest(BaseModel):
    """Request payload to connect a provider for a project/session."""
    session_id: str
    project_id: str
    provider_id: str
    credentials: Dict[str, str]


class CredentialType(str, Enum):
    """Types of credentials supported."""
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    CONNECTION_STRING = "connection_string"
    USERNAME_PASSWORD = "username_password"


class CredentialField(BaseModel):
    """Definition of a credential field."""
    name: str
    display_name: str
    type: CredentialType
    required: bool = True
    description: Optional[str] = None
    placeholder: Optional[str] = None
    sensitive: bool = True  # Should be masked in UI


class ToolProvider(BaseModel):
    """Definition of a tool provider (e.g., Salesforce, AWS, Azure)."""
    provider_id: str  # e.g., "salesforce", "aws_s3"
    display_name: str
    description: str
    icon_url: Optional[str] = None
    credential_fields: List[CredentialField]
    documentation_url: Optional[str] = None


class ToolDefinition(BaseModel):
    """Declarative tool definition."""
    tool_id: str  # e.g., "salesforce_create_lead"
    display_name: str
    description: str
    provider_id: str  # Links to ToolProvider
    agent_type: str  # Which agent uses this tool
    requires_credentials: bool = True
    parameters: Dict[str, Any] = Field(default_factory=dict)


class CredentialBinding(BaseModel):
    """Binding of credentials to a project."""
    project_id: str
    provider_id: str
    secret_uri: str  # Key Vault secret URI
    created_at: str
    is_active: bool = True


# Registry of all available providers
TOOL_PROVIDERS: Dict[str, ToolProvider] = {}

# Helper to ensure provider_id uniqueness


def register_provider(provider: ToolProvider):
    if provider.provider_id in TOOL_PROVIDERS:
        raise ValueError(f"Duplicate provider_id: {provider.provider_id}")
    TOOL_PROVIDERS[provider.provider_id] = provider


register_provider(ToolProvider(
    provider_id="salesforce",
    display_name="Salesforce",
    description="CRM and customer management platform",
    credential_fields=[
        CredentialField(
            name="instance_url",
            display_name="Instance URL",
            type=CredentialType.API_KEY,
            description="Your Salesforce instance URL (e.g., https://yourcompany.salesforce.com)",
            placeholder="https://yourcompany.salesforce.com",
            sensitive=False,
        ),
        CredentialField(
            name="access_token",
            display_name="Access Token",
            type=CredentialType.API_KEY,
            description="OAuth access token for Salesforce API",
            sensitive=True,
        ),
    ],
    documentation_url="https://developer.salesforce.com/docs/apis",
))

register_provider(ToolProvider(
    provider_id="aws_s3",
    display_name="AWS S3",
    description="Amazon S3 object storage",
    credential_fields=[
        CredentialField(
            name="region",
            display_name="AWS Region",
            type=CredentialType.API_KEY,
            placeholder="us-east-1",
            required=False,
            sensitive=False,
        ),
    ],
))

register_provider(ToolProvider(
    provider_id="firestore",
    display_name="Google Firestore",
    description="NoSQL document database",
    credential_fields=[
        CredentialField(
            name="service_account_json",
            display_name="Service Account JSON",
            type=CredentialType.API_KEY,
            description="Google Cloud service account credentials",
            sensitive=True,
        ),
        CredentialField(
            name="project_id",
            display_name="Project ID",
            type=CredentialType.API_KEY,
            sensitive=False,
        ),
    ],
))

register_provider(ToolProvider(
    provider_id="microsoft_365",
    display_name="Microsoft 365",
    description="Microsoft 365 suite (Outlook, Teams, SharePoint)",
    credential_fields=[
        CredentialField(
            name="tenant_id",
            display_name="Tenant ID",
            type=CredentialType.API_KEY,
            sensitive=False,
        ),
        CredentialField(
            name="client_id",
            display_name="Client ID",
            type=CredentialType.API_KEY,
            sensitive=False,
        ),
        CredentialField(
            name="client_secret",
            display_name="Client Secret",
            type=CredentialType.API_KEY,
            sensitive=True,
        ),
    ],
    documentation_url="https://learn.microsoft.com/en-us/graph/",
))

register_provider(ToolProvider(
    provider_id="google_workspace",
    display_name="Google Workspace",
    description="Google Workspace (Gmail, Drive, Calendar)",
    credential_fields=[
        CredentialField(
            name="service_account_json",
            display_name="Service Account JSON",
            type=CredentialType.API_KEY,
            sensitive=True,
        ),
    ],
    documentation_url="https://developers.google.com/workspace",
))


# Registry of available tools
TOOL_DEFINITIONS: Dict[str, ToolDefinition] = {}

# Helper to ensure tool_id uniqueness and required fields


def register_tool(tool: ToolDefinition):
    if tool.tool_id in TOOL_DEFINITIONS:
        raise ValueError(f"Duplicate tool_id: {tool.tool_id}")
    if not tool.agent_type:
        raise ValueError(f"Tool {tool.tool_id} missing agent_type")
    if not hasattr(tool, "requires_credentials"):
        raise ValueError(f"Tool {tool.tool_id} missing requires_credentials")
    TOOL_DEFINITIONS[tool.tool_id] = tool


register_tool(ToolDefinition(
    tool_id="salesforce_create_lead",
    display_name="Create Salesforce Lead",
    description="Create a new lead in Salesforce CRM",
    provider_id="salesforce",
    agent_type="Marketing_Agent",
    requires_credentials=True,
))

register_tool(ToolDefinition(
    tool_id="s3_upload_file",
    display_name="Upload to S3",
    description="Upload a file to AWS S3 bucket",
    provider_id="aws_s3",
    agent_type="Generic_Agent",
    requires_credentials=False,
))

register_tool(ToolDefinition(
    tool_id="get_video_signed_url",
    display_name="Get Video Signed URL",
    description="Generate a signed CloudFront URL for a video S3 key",
    provider_id="aws_s3",
    agent_type="Tech_Support_Agent",
    requires_credentials=False,
))

register_tool(ToolDefinition(
    tool_id="read_firestore_doc",
    display_name="Read Firestore Document",
    description="Read a Firestore document by path",
    provider_id="firestore",
    agent_type="Tech_Support_Agent",
    requires_credentials=True,
))

register_tool(ToolDefinition(
    tool_id="write_firestore_doc",
    display_name="Write Firestore Document",
    description="Write a Firestore document by path",
    provider_id="firestore",
    agent_type="Tech_Support_Agent",
    requires_credentials=True,
))

register_tool(ToolDefinition(
    tool_id="list_firestore_collections",
    display_name="List Firestore Collections",
    description="List all collections in Firestore",
    provider_id="firestore",
    agent_type="Tech_Support_Agent",
    requires_credentials=True,
))

register_tool(ToolDefinition(
    tool_id="list_firestore_documents",
    display_name="List Firestore Documents",
    description="List documents in a Firestore collection",
    provider_id="firestore",
    agent_type="Tech_Support_Agent",
    requires_credentials=True,
))

register_tool(ToolDefinition(
    tool_id="count_firestore_docs",
    display_name="Count Firestore Documents",
    description="Count documents in a Firestore collection",
    provider_id="firestore",
    agent_type="Tech_Support_Agent",
    requires_credentials=True,
))


register_tool(ToolDefinition(
    tool_id="query_firestore_docs",
    display_name="Query Firestore Documents",
    description="Query Firestore documents with filters",
    provider_id="firestore",
    agent_type="Tech_Support_Agent",
    requires_credentials=True,
))

register_tool(ToolDefinition(
    tool_id="s3_upload_object",
    display_name="Upload Object to S3",
    description="Upload an object to AWS S3 bucket",
    provider_id="aws_s3",
    agent_type="Tech_Support_Agent",
    requires_credentials=False,
))

register_tool(ToolDefinition(
    tool_id="s3_delete_object",
    display_name="Delete Object from S3",
    description="Delete an object from AWS S3 bucket",
    provider_id="aws_s3",
    agent_type="Tech_Support_Agent",
    requires_credentials=False,
))

register_tool(ToolDefinition(
    tool_id="s3_list_objects",
    display_name="List S3 Objects",
    description="List objects in an AWS S3 bucket",
    provider_id="aws_s3",
    agent_type="Tech_Support_Agent",
    requires_credentials=False,
))

register_tool(ToolDefinition(
    tool_id="update_firestore_doc",
    display_name="Update Firestore Document",
    description="Update an existing Firestore document by path",
    provider_id="firestore",
    agent_type="Tech_Support_Agent",
    requires_credentials=True,
))

register_tool(ToolDefinition(
    tool_id="delete_firestore_doc",
    display_name="Delete Firestore Document",
    description="Delete an existing Firestore document by path",
    provider_id="firestore",
    agent_type="Tech_Support_Agent",
    requires_credentials=True,
))


# Credential binding store (in-memory for now)
_CREDENTIAL_BINDINGS: Dict[str, CredentialBinding] = {}


def save_credential_binding(binding: CredentialBinding):
    key = f"{binding.project_id}:{binding.provider_id}"
    _CREDENTIAL_BINDINGS[key] = binding


def get_credential_binding(project_id: str, provider_id: str) -> Optional[CredentialBinding]:
    key = f"{project_id}:{provider_id}"
    return _CREDENTIAL_BINDINGS.get(key)


def get_bindings_for_project(project_id: str) -> List[CredentialBinding]:
    return [b for k, b in _CREDENTIAL_BINDINGS.items() if b.project_id == project_id]


class ToolRegistry:
    """Central registry for tool discovery."""

    @staticmethod
    def get_all_providers() -> List[ToolProvider]:
        """Get all available tool providers."""
        return list(TOOL_PROVIDERS.values())

    @staticmethod
    def get_provider(provider_id: str) -> Optional[ToolProvider]:
        """Get a specific provider by ID."""
        return TOOL_PROVIDERS.get(provider_id)

    @staticmethod
    def get_tools_for_agent(agent_type: str) -> List[ToolDefinition]:
        """Get all tools available for a specific agent."""
        return [
            tool for tool in TOOL_DEFINITIONS.values()
            if tool.agent_type == agent_type
        ]

    @staticmethod
    def get_all_tools() -> List[ToolDefinition]:
        """Get all registered tools."""
        return list(TOOL_DEFINITIONS.values())

    @staticmethod
    def _expand_enabled_tools(enabled_tools: Optional[List[str]]) -> set[str]:
        """Expand enabled tool aliases/provider ids into concrete tool_ids."""
        enabled = set(enabled_tools or [])
        if not enabled:
            return set()

        expanded: set[str] = set()
        provider_ids = {p.provider_id for p in ToolRegistry.get_all_providers()}

        legacy_alias_to_provider = {
            "firestore_rw": "firestore",
            "aws_s3": "aws_s3",
            "s3_uploader": "aws_s3",
            "s3_upload_file": "aws_s3",
            "get_video_signed_url": "aws_s3",
        }

        for token in enabled:
            # Exact tool id
            if token in TOOL_DEFINITIONS:
                expanded.add(token)
                continue

            # Provider id
            provider_id = token if token in provider_ids else legacy_alias_to_provider.get(token)
            if provider_id:
                for tool in TOOL_DEFINITIONS.values():
                    if tool.provider_id == provider_id:
                        expanded.add(tool.tool_id)

        return expanded

    @staticmethod
    def get_tools_for_agent_and_profile(
        agent_type: str,
        enabled_tools: Optional[List[str]],
        active_providers: Optional[List[str]] = None,
    ) -> List[ToolDefinition]:
        """Get tools for an agent filtered by project configuration/policies."""
        candidate_tools = ToolRegistry.get_tools_for_agent(agent_type)
        if not candidate_tools:
            return []

        expanded_enabled = ToolRegistry._expand_enabled_tools(enabled_tools)
        active_provider_set = set(active_providers or [])

        # Backward-compatible default: if nothing configured, expose agent's declared tools.
        if not expanded_enabled and not active_provider_set:
            return candidate_tools

        filtered: List[ToolDefinition] = []
        for tool in candidate_tools:
            if tool.tool_id in expanded_enabled or tool.provider_id in active_provider_set:
                filtered.append(tool)
        return filtered

    @staticmethod
    def get_required_credentials(tool_id: str) -> Optional[List[CredentialField]]:
        """Get required credentials for a tool."""
        tool = TOOL_DEFINITIONS.get(tool_id)
        if not tool:
            return None

        provider = TOOL_PROVIDERS.get(tool.provider_id)
        return provider.credential_fields if provider else None
