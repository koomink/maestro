import pytest

from maestro.credentials import CredentialResolver


def test_env_credential_resolver_reads_values(monkeypatch):
    monkeypatch.setenv("MAESTRO_TEST_SECRET", "super-secret-value")

    resolver = CredentialResolver()

    assert resolver.get("MAESTRO_TEST_SECRET") == "super-secret-value"
    assert resolver.require("MAESTRO_TEST_SECRET") == "super-secret-value"
    assert resolver.present("MAESTRO_TEST_SECRET") is True


def test_env_credential_resolver_missing_error_names_ref_not_value(monkeypatch):
    monkeypatch.delenv("MAESTRO_MISSING_SECRET", raising=False)

    resolver = CredentialResolver()

    with pytest.raises(ValueError) as exc_info:
        resolver.require("MAESTRO_MISSING_SECRET")

    message = str(exc_info.value)
    assert "MAESTRO_MISSING_SECRET" in message
    assert "secret-value" not in message


def test_env_credential_resolver_masks_without_exposing_secret(monkeypatch):
    monkeypatch.setenv("MAESTRO_TEST_SECRET", "abcdef1234567890")

    resolver = CredentialResolver()

    masked = resolver.mask("MAESTRO_TEST_SECRET")
    assert masked is not None
    assert "abcdef1234567890" not in masked
    assert masked.startswith("ab")
    assert masked.endswith("90")

