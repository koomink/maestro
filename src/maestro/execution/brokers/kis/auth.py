import fcntl
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from maestro.config.models import KISConfig
from maestro.core.clock import utc_now
from maestro.credentials import DEFAULT_CREDENTIAL_RESOLVER, CredentialResolver

KIS_DATETIME_TZ = ZoneInfo("Asia/Seoul")


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


@dataclass(frozen=True)
class KISApprovalKey:
    approval_key: str
    source: str


class KISAuthManager:
    def __init__(
        self,
        config: KISConfig,
        transport: KISTransport | None = None,
        *,
        credential_resolver: CredentialResolver | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.credential_resolver = credential_resolver or DEFAULT_CREDENTIAL_RESOLVER

    def validate_readonly_credentials(self) -> None:
        if self.config.provider == "mock":
            return
        missing = [
            env_name
            for env_name in [self.config.app_key_env, self.config.app_secret_env]
            if not self.credential_resolver.present(env_name)
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
        app_key = self.credential_resolver.require(self.config.app_key_env)
        app_secret = self.credential_resolver.require(self.config.app_secret_env)
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
        token_from_env = self.credential_resolver.get(self.config.access_token_env)
        if token_from_env:
            return KISToken(access_token=token_from_env, expires_at=None, source="env")

        cached = self._read_cached_token()
        if cached is not None and cached.is_valid():
            return cached

        if self.config.token_cache_path is not None:
            lock_path = Path(f"{self.config.token_cache_path}.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
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
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        if self.transport is None:
            raise ValueError(
                "KIS access token is unavailable. Set the configured access token "
                "environment variable or provide a token-capable transport."
            )
        token = self._issue_access_token()
        self._write_cached_token(token)
        return token

    def get_websocket_approval_key(self) -> KISApprovalKey:
        if self.config.provider == "mock":
            return KISApprovalKey(approval_key="mock-approval-key", source="mock")
        approval_key_from_env = self.credential_resolver.get(self.config.approval_key_env)
        if approval_key_from_env:
            return KISApprovalKey(approval_key=approval_key_from_env, source="env")
        if self.transport is None:
            raise ValueError(
                "KIS websocket approval key is unavailable. Set the configured approval "
                "key environment variable or provide an approval-key-capable transport."
            )
        return self._issue_websocket_approval_key()

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
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "text/plain",
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

    def _issue_websocket_approval_key(self) -> KISApprovalKey:
        credentials = self.get_credentials()
        base_url = self.config.resolved_base_url()
        payload = {
            "grant_type": "client_credentials",
            "appkey": credentials.app_key,
            "secretkey": credentials.app_secret,
        }
        response = self.transport.request(
            "POST",
            f"{base_url}/oauth2/Approval",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "text/plain",
            },
            json_body=payload,
            timeout_seconds=self.config.timeout_seconds,
        )
        approval_key = response.get("approval_key")
        if not approval_key:
            raise ValueError("KIS approval key response did not include an approval key")
        return KISApprovalKey(approval_key=str(approval_key), source="oauth")

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
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(json.dumps(payload))
        temp_path.chmod(0o600)
        os.replace(temp_path, path)
        path.chmod(0o600)

    def _account_id(self) -> str | None:
        if self.config.account_id:
            return self.config.account_id
        if self.config.account_id_env:
            return self.credential_resolver.get(self.config.account_id_env)
        return None


def _parse_kis_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo:
                return parsed.astimezone(tz=UTC)
            return parsed.replace(tzinfo=KIS_DATETIME_TZ).astimezone(tz=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo:
        return parsed.astimezone(tz=UTC)
    return parsed.replace(tzinfo=KIS_DATETIME_TZ).astimezone(tz=UTC)
