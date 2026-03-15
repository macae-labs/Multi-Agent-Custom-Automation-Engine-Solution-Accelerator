import os
import sys
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

# Mock Azure dependencies to prevent import errors
sys.modules["azure.monitor"] = MagicMock()
sys.modules["azure.monitor.events.extension"] = MagicMock()
sys.modules["azure.monitor.opentelemetry"] = MagicMock()
# Mock SK OpenAI connector import used by app_config to avoid SDK mismatch at collection time
sys.modules["semantic_kernel.connectors.ai.open_ai"] = MagicMock()

# Mock environment variables before importing app
os.environ["COSMOSDB_ENDPOINT"] = "https://mock-endpoint.documents.azure.com:443/"
os.environ["COSMOSDB_DATABASE"] = "mock-database"
os.environ["COSMOSDB_CONTAINER"] = "mock-container"
os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"] = (
    "InstrumentationKey=mock-instrumentation-key;IngestionEndpoint=https://mock-ingestion-endpoint"
)
os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"] = "mock-deployment-name"
os.environ["AZURE_OPENAI_API_VERSION"] = "2024-11-20"
os.environ["AZURE_OPENAI_ENDPOINT"] = "https://mock-openai-endpoint.azure.com/"
os.environ["AZURE_AI_SUBSCRIPTION_ID"] = "mock-subscription-id"
os.environ["AZURE_AI_RESOURCE_GROUP"] = "mock-resource-group"
os.environ["AZURE_AI_PROJECT_NAME"] = "mock-project-name"
os.environ["AZURE_AI_AGENT_ENDPOINT"] = "https://mock-agent-endpoint.azure.com/"

# Mock telemetry initialization to prevent errors
with patch("azure.monitor.opentelemetry.configure_azure_monitor", MagicMock()):
    from app_kernel import app

# Initialize FastAPI test client
client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_dependencies(monkeypatch):
    """Mock dependencies to simplify tests."""
    monkeypatch.setattr(
        "auth.auth_utils.get_authenticated_user_details",
        lambda headers: {"user_principal_id": "mock-user-id"},
    )


def test_input_task_invalid_json():
    """Test the case where the input JSON is invalid."""
    invalid_json = "Invalid JSON data"

    headers = {
        "Authorization": "Bearer mock-token",
        "Content-Type": "application/json",
    }
    response = client.post("/api/input_task", content=invalid_json, headers=headers)

    # Assert response for invalid JSON
    assert response.status_code == 422
    assert "detail" in response.json()


def test_input_task_missing_description():
    """Test the case where the input task description is missing."""
    input_task = {
        "session_id": None,
        "user_id": "mock-user-id",
    }

    headers = {"Authorization": "Bearer mock-token"}
    response = client.post("/api/input_task", json=input_task, headers=headers)

    # Assert response for missing description
    assert response.status_code == 422
    assert "detail" in response.json()


def test_basic_endpoint():
    """Test a basic endpoint to ensure the app runs."""
    response = client.get("/")
    assert response.status_code == 404  # The root endpoint is not defined


def test_input_task_empty_description():
    """Tests if /api/input_task handles an empty description."""
    empty_task = {"session_id": None, "user_id": "mock-user-id", "description": ""}
    headers = {"Authorization": "Bearer mock-token"}
    response = client.post("/api/input_task", json=empty_task, headers=headers)

    assert response.status_code == 422
    assert "detail" in response.json()  # Assert error message for missing description


if __name__ == "__main__":
    pytest.main()
