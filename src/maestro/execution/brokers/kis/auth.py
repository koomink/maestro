import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from maestro.config.models import KISConfig
from maestro.core.clock import utc_now


class KISTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(frozen=True)
class KISCredentials:
    app_key: str
    app_secret: str
    account_id: str

    @property
    def cano(self) -> str:
        return self.account_id.replace("-", "")[:8]

    @property
    def account_product_code(self) -> str:
        return self.account_id.replace("-", "")[8:10]


@dataclass(frozen=True)
class KISToken:
    access_token: str
    expires_at: datetime | None
    source: str

    def is_valid(self) -> bool:
        if self.expires_at is None:
            return True
        return self.expires_at > utc_now() + timedelta(minutes=5)


class KISAuthManager:
    def __init__(self, config: KISConfig, transport: KISTransport | None = None) -> None:
        self.config = config
        self.transport = transport

    def validate_readonly_credentials(self) -> None:
        if self.config.provider == "mock":
            return
        missing = [
            env_name
            for env_name in [self.config.app_key_env, self.config.app_secret_env]
            if not os.getenv(env_name)
        ]
        if missing:
            raise ValueError(f"Missing KIS credential environment variables: {missing}")
        account_id = self._account_id()
        if not account_id:
            raise ValueError(
                "KIS account_id is required for live_readonly provider. Set account_id "
                "or the configured account_id_env environment variable."
            )
        if len(account_id.replace("-", "")) < 10:
            raise ValueError("KIS account_id must include account number and product code")

    def get_credentials(self) -> KISCredentials:
        self.validate_readonly_credentials()
        app_key = os.environ[self.config.app_key_env]
        app_secret = os.environ[self.config.app_secret_env]
        account_id = self._account_id()
        if account_id is None:
            raise ValueError("KIS account_id is required for live_readonly provider")
        return KISCredentials(
            app_key=app_key,
            app_secret=app_secret,
            account_id=account_id,
        )

    def get_access_token(self) -> KISToken:
        if self.config.provider == "mock":
            return KISToken(access_token="mock-token", expires_at=None, source="mock")
        token_from_env = os.getenv(self.config.access_token_env)
        if token_from_env:
            return KISToken(access_token=token_from_env, expires_at=None, source="env")

        cached = self._read_cached_token()
        if cached is not None and cached.is_valid():
            return cached

        if self.transport is None:
            raise ValueError(
                "KIS access token is unavailable. Set the configured access token "
                "environment variable or provide a token-capable transport."
            )
        token = self._issue_access_token()
        self._write_cached_token(token)
        return token

    def _issue_access_token(self) -> KISToken:
        credentials = self.get_credentials()
        base_url = self.config.resolved_base_url()
        payload = {
            "grant_type": "client_credentials",
            "appkey": credentials.app_key,
            "appsecret": credentials.app_secret,
        }
        response = self.transport.request(
            "POST",
            f"{base_url}/oauth2/tokenP",
            headers={
                "Content-Type": "application/json",
                "Accept": "text/plain",
                "charset": "UTF-8",
            },
            json_body=payload,
            timeout_seconds=self.config.timeout_seconds,
        )
        access_token = response.get("access_token")
        if not access_token:
            raise ValueError("KIS token response did not include an access token")
        return KISToken(
            access_token=str(access_token),
            expires_at=_parse_kis_datetime(response.get("access_token_token_expired")),
            source="oauth",
        )

    def _read_cached_token(self) -> KISToken | None:
        if self.config.token_cache_path is None:
            return None
        path = Path(self.config.token_cache_path)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        token = payload.get("access_token")
        if not token:
            return None
        return KISToken(
            access_token=str(token),
            expires_at=_parse_kis_datetime(payload.get("expires_at")),
            source="cache",
        )

    def _write_cached_token(self, token: KISToken) -> None:
        if self.config.token_cache_path is None:
            return
        path = Path(self.config.token_cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "access_token": token.access_token,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)

    def _account_id(self) -> str | None:
        if self.config.account_id:
            return self.config.account_id
        if self.config.account_id_env:
            return os.getenv(self.config.account_id_env)
        return None


def _parse_kis_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    tzinfo = utc_now().tzinfo
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.astimezone(tz=tzinfo) if parsed.tzinfo else parsed.replace(tzinfo=tzinfo)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(tz=tzinfo) if parsed.tzinfo else parsed.replace(tzinfo=tzinfo)
