"""
Configuration settings for the MCP server.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPServerConfig(BaseSettings):
    """MCP Server configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # This will ignore extra environment variables
    )

    # Server settings
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=9000)
    debug: bool = Field(default=False)

    # Authentication settings
    tenant_id: str | None = Field(default=None)
    client_id: str | None = Field(default=None)
    jwks_uri: str | None = Field(default=None)
    issuer: str | None = Field(default=None)
    audience: str | None = Field(default=None)

    # MCP specific settings
    server_name: str = Field(default="MacaeMcpServer")
    enable_auth: bool = Field(default=True)

    # Dataset path - added to handle the environment variable
    dataset_path: str = Field(default="./datasets")


# Global configuration instance
config = MCPServerConfig()


def get_auth_config():
    """Get authentication configuration for Azure."""
    if not config.enable_auth:
        return None

    return {
        "tenant_id": config.tenant_id,
        "client_id": config.client_id,
        "jwks_uri": config.jwks_uri,
        "issuer": config.issuer,
        "audience": config.audience,
    }


def get_server_config():
    """Get server configuration."""
    return {"host": config.host, "port": config.port, "debug": config.debug}
