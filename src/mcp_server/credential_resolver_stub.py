"""Type stub for credential_resolver from backend."""


class CredentialResolver:
    """Stub for credential resolver to avoid import errors."""

    async def resolve_by_secret_ref(self, secret_ref: str) -> dict[str, str] | None:
        """Resolve credentials from Key Vault.

        Args:
            secret_ref: Secret reference or name

        Returns:
            Dictionary with credentials or None
        """
        ...


# Singleton instance stub
credential_resolver: CredentialResolver | None = None
