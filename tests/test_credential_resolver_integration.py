import pytest

from maestro.config.broker import KISConfig
from maestro.credentials import CredentialResolver
from maestro.datahub.fred_provider import FREDDataProvider
from maestro.datahub.newsapi_provider import NewsAPINewsProvider
from maestro.execution.brokers.kis.auth import KISAuthManager
from maestro.integrations.telegram.bot import TelegramBotAPIClient


class RecordingResolver(CredentialResolver):
    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})
        self.requested: list[str] = []

    def get(self, name: str) -> str | None:
        self.requested.append(name)
        return self.values.get(name)


def test_kis_auth_manager_reads_credentials_through_resolver():
    resolver = RecordingResolver(
        {
            "KIS_APP_KEY": "app-key",
            "KIS_APP_SECRET": "app-secret",
            "KIS_ACCOUNT_ID": "12345678-01",
            "KIS_ACCESS_TOKEN": "access-token",
            "KIS_APPROVAL_KEY": "approval-key",
        }
    )
    config = KISConfig(
        enabled=True,
        provider="kis",
        account_id_env="KIS_ACCOUNT_ID",
        app_key_env="KIS_APP_KEY",
        app_secret_env="KIS_APP_SECRET",
        access_token_env="KIS_ACCESS_TOKEN",
        approval_key_env="KIS_APPROVAL_KEY",
    )

    manager = KISAuthManager(config, credential_resolver=resolver)

    credentials = manager.get_credentials()
    token = manager.get_access_token()
    approval_key = manager.get_websocket_approval_key()

    assert credentials.app_key == "app-key"
    assert credentials.app_secret == "app-secret"
    assert credentials.account_id == "12345678-01"
    assert token.access_token == "access-token"
    assert approval_key.approval_key == "approval-key"
    assert "KIS_APP_KEY" in resolver.requested
    assert "KIS_APP_SECRET" in resolver.requested
    assert "KIS_ACCOUNT_ID" in resolver.requested


def test_datahub_providers_read_api_keys_through_resolver():
    resolver = RecordingResolver({"FRED_KEY": "fred-secret", "NEWS_KEY": "news-secret"})

    fred = FREDDataProvider(api_key_env="FRED_KEY", credential_resolver=resolver)
    news = NewsAPINewsProvider(api_key_env="NEWS_KEY", credential_resolver=resolver)

    assert fred._api_key() == "fred-secret"
    assert news._api_key() == "news-secret"
    assert "FRED_KEY" in resolver.requested
    assert "NEWS_KEY" in resolver.requested


def test_telegram_client_reads_token_through_resolver():
    resolver = RecordingResolver({"TELEGRAM_TOKEN": "telegram-secret"})

    client = TelegramBotAPIClient(
        token_env="TELEGRAM_TOKEN",
        credential_resolver=resolver,
    )

    assert client.base_url == "https://api.telegram.org/bottelegram-secret"
    assert resolver.requested == ["TELEGRAM_TOKEN"]


def test_telegram_client_missing_token_error_names_ref_only():
    resolver = RecordingResolver()

    with pytest.raises(ValueError) as exc_info:
        TelegramBotAPIClient(token_env="TELEGRAM_TOKEN", credential_resolver=resolver)

    assert "TELEGRAM_TOKEN" in str(exc_info.value)
    assert "telegram-secret" not in str(exc_info.value)
