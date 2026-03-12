"""Credential resolution service using Azure Key Vault."""

import json
import logging
from typing import Dict, Optional
from azure.keyvault.secrets.aio import SecretClient
from app_config import config


class CredentialResolver:
    """Resolves credentials from Key Vault at runtime."""

    def __init__(self):
        self._kv_client: Optional[SecretClient] = None
        self._cache: Dict[str, Dict[str, str]] = {}

    async def initialize(self) -> None:
        """Pre-warm the Key Vault client during app startup."""
        try:
            _ = self._get_keyvault_client()
            logging.info("CredentialResolver initialized - Key Vault client ready")
        except Exception as exc:
            logging.warning(f"CredentialResolver initialization skipped: {exc}")

    def _get_keyvault_client(self) -> SecretClient:
        """Lazy initialization of Key Vault client."""
        if self._kv_client is None:
            # Use Key Vault URL from config or default
            kv_url = config._get_optional(
                "AZURE_KEY_VAULT_URL",
                "https://yellowstkeyvault8df0efc3.vault.azure.net/",
            )
            if not kv_url:
                raise ValueError("AZURE_KEY_VAULT_URL not configured")

            credential = config.get_azure_credentials()
            self._kv_client = SecretClient(vault_url=kv_url, credential=credential)

        return self._kv_client

    async def resolve_credentials(
        self, project_id: str, provider_id: str
    ) -> Optional[Dict[str, str]]:
        """Resolve credentials for a project/provider from Key Vault.

        Args:
            project_id: The project ID
            provider_id: The provider ID (e.g., "salesforce")

        Returns:
            Dict of credential key-value pairs, or None if not found
        """
        cache_key = f"{project_id}:{provider_id}"

        # Check cache first
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            # Secret name format: project-{project_id}-{provider_id}
            secret_name = f"project-{project_id}-{provider_id}".replace("_", "-")

            kv_client = self._get_keyvault_client()
            secret = await kv_client.get_secret(secret_name)

            # Parse JSON credentials, handle None value
            if secret.value is None:
                logging.warning(f"Secret value for {secret_name} is None")
                return None
            credentials = json.loads(secret.value)
            # Cache for this session
            self._cache[cache_key] = credentials
            logging.info(
                f"Resolved credentials for {provider_id} in project {project_id}"
            )
            return credentials

        except Exception as e:
            logging.warning(f"Failed to resolve credentials for {provider_id}: {e}")
            return None

    async def store_credentials(
        self, project_id: str, provider_id: str, credentials: Dict[str, str]
    ) -> str:
        """Store credentials in Key Vault and return secret URI.

        Args:
            project_id: The project ID
            provider_id: The provider ID
            credentials: Dict of credential key-value pairs

        Returns:
            Secret URI in Key Vault
        """
        try:
            secret_name = f"project-{project_id}-{provider_id}".replace("_", "-")

            kv_client = self._get_keyvault_client()
            secret = await kv_client.set_secret(secret_name, json.dumps(credentials))

            # Invalidate cache
            cache_key = f"{project_id}:{provider_id}"
            self._cache.pop(cache_key, None)

            logging.info(
                f"Stored credentials for {provider_id} in project {project_id}"
            )
            if secret.id is None:
                raise RuntimeError(
                    f"Secret URI for {provider_id} in project {project_id} is None"
                )
            return secret.id
        except Exception as e:
            logging.error(f"Failed to store credentials for {provider_id}: {e}")
            raise

    async def close(self) -> None:
        """Close async clients held by the resolver to avoid aiohttp leaks."""
        if self._kv_client is not None:
            try:
                await self._kv_client.close()
                logging.info("CredentialResolver SecretClient closed successfully")
            except Exception as exc:
                logging.warning(
                    "Error closing CredentialResolver SecretClient: %s", exc
                )
            finally:
                self._kv_client = None


# Global singleton
credential_resolver = CredentialResolver()
