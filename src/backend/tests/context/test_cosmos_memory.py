import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from context.cosmos_memory_kernel import CosmosMemoryContext


@pytest.fixture
def mock_env_variables(monkeypatch):
    """Mock all required environment variables."""
    env_vars = {
        "COSMOSDB_ENDPOINT": "https://mock-endpoint.documents.azure.com:443/",
        "COSMOSDB_DATABASE": "mock-database",
        "COSMOSDB_CONTAINER": "mock-container",
        "AZURE_OPENAI_DEPLOYMENT_NAME": "mock-deployment-name",
        "AZURE_OPENAI_API_VERSION": "2024-11-20",
        "AZURE_OPENAI_ENDPOINT": "https://mock-openai-endpoint.azure.com/",
        "AZURE_AI_SUBSCRIPTION_ID": "mock-subscription-id",
        "AZURE_AI_RESOURCE_GROUP": "mock-resource-group",
        "AZURE_AI_PROJECT_NAME": "mock-project-name",
        "AZURE_AI_AGENT_ENDPOINT": "https://mock-agent-endpoint.azure.com/",
    }
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)


@pytest.mark.asyncio
async def test_cosmos_memory_context_initialization(mock_env_variables):
    """Test CosmosMemoryContext initialization."""
    with patch('app_config.AppConfig.get_cosmos_database_client') as mock_db_client:
        mock_container = AsyncMock()
        mock_database = MagicMock()
        mock_database.get_container_client.return_value = mock_container
        mock_db_client.return_value = mock_database
        
        context = CosmosMemoryContext(
            session_id="test_session",
            user_id="test_user"
        )
        
        assert context.session_id == "test_session"
        assert context.user_id == "test_user"
