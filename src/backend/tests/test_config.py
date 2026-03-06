# tests/test_config.py
from collections.abc import Iterator
from unittest.mock import patch
import os
import pytest

# Mock environment variables globally
MOCK_ENV_VARS = {
    "COSMOSDB_ENDPOINT": "https://mock-cosmosdb.documents.azure.com:443/",
    "COSMOSDB_DATABASE": "mock_database",
    "COSMOSDB_CONTAINER": "mock_container",
    "AZURE_OPENAI_DEPLOYMENT_NAME": "mock-deployment",
    "AZURE_OPENAI_API_VERSION": "2024-05-01-preview",
    "AZURE_OPENAI_ENDPOINT": "https://mock-openai-endpoint.azure.com/",
    "AZURE_TENANT_ID": "mock-tenant-id",
    "AZURE_CLIENT_ID": "mock-client-id",
    "AZURE_CLIENT_SECRET": "mock-client-secret",
    "AZURE_AI_SUBSCRIPTION_ID": "mock-subscription-id",
    "AZURE_AI_RESOURCE_GROUP": "mock-resource-group",
    "AZURE_AI_PROJECT_NAME": "mock-project-name",
    "AZURE_AI_AGENT_ENDPOINT": "https://mock-agent-endpoint.azure.com/",
}


@pytest.fixture
def mock_env() -> Iterator[None]:
    with patch.dict(os.environ, MOCK_ENV_VARS):
        yield


def test_app_config_initialization(mock_env):
    """Test AppConfig initialization with environment variables."""
    from app_config import AppConfig
    
    config = AppConfig()
    assert config.COSMOSDB_ENDPOINT == MOCK_ENV_VARS["COSMOSDB_ENDPOINT"]
    assert config.COSMOSDB_DATABASE == MOCK_ENV_VARS["COSMOSDB_DATABASE"]
    assert config.AZURE_OPENAI_DEPLOYMENT_NAME == MOCK_ENV_VARS["AZURE_OPENAI_DEPLOYMENT_NAME"]


def test_get_required_config(mock_env):
    """Test _get_required method."""
    from app_config import AppConfig
    
    config = AppConfig()
    assert config._get_required("COSMOSDB_ENDPOINT") == MOCK_ENV_VARS["COSMOSDB_ENDPOINT"]


def test_get_optional_config(mock_env):
    """Test _get_optional method."""
    from app_config import AppConfig
    
    config = AppConfig()
    assert config._get_optional("NON_EXISTENT_VAR", "default_value") == "default_value"
    assert config._get_optional("COSMOSDB_DATABASE", "default_db") == MOCK_ENV_VARS["COSMOSDB_DATABASE"]


def test_get_bool_config(mock_env):
    """Test _get_bool method."""
    from app_config import AppConfig
    
    with patch.dict(os.environ, {"FEATURE_ENABLED": "true"}):
        config = AppConfig()
        assert config._get_bool("FEATURE_ENABLED") is True
    
    with patch.dict(os.environ, {"FEATURE_ENABLED": "false"}):
        config = AppConfig()
        assert config._get_bool("FEATURE_ENABLED") is False
