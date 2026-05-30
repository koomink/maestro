import os


class CredentialResolver:
    """Resolve credential references from the process environment."""

    def get(self, name: str) -> str | None:
        value = os.getenv(name)
        return value or None

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise ValueError(f"Credential is not set: {name}")
        return value

    def present(self, name: str) -> bool:
        return self.get(name) is not None

    def mask(self, name: str) -> str | None:
        value = self.get(name)
        if value is None:
            return None
        if len(value) <= 4:
            return "[REDACTED]"
        return f"{value[:2]}...[REDACTED]...{value[-2:]}"


DEFAULT_CREDENTIAL_RESOLVER = CredentialResolver()


__all__ = ["CredentialResolver", "DEFAULT_CREDENTIAL_RESOLVER"]
