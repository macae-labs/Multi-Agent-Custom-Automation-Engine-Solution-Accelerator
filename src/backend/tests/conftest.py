import os

# Set default environment variables for testing to prevent import-time failures
# in app_config.py when its global `config = AppConfig()` is instantiated during
# test collection. These values are overridden by individual test fixtures as needed.
os.environ.setdefault(
    "AZURE_OPENAI_ENDPOINT", "https://mock-openai-endpoint.azure.com/"
)
os.environ.setdefault("AZURE_AI_SUBSCRIPTION_ID", "mock-subscription-id")
os.environ.setdefault("AZURE_AI_RESOURCE_GROUP", "mock-resource-group")
os.environ.setdefault("AZURE_AI_PROJECT_NAME", "mock-project-name")
os.environ.setdefault(
    "AZURE_AI_AGENT_ENDPOINT", "https://mock-agent-endpoint.azure.com/"
)
