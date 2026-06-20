import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from maestro.config.broker import BrokerAccountConfig
from maestro.core.clock import utc_now


class TossAuthManager:
    def __init__(self, config: BrokerAccountConfig, *, opener: Any | None = None) -> None:
        self.config = config
        self.opener = opener or build_opener()

    def access_token(self) -> str:
        if self.config.access_token_env:
            token = os.getenv(self.config.access_token_env)
            if token:
                return token
        cached = self._load_cached_token()
        if cached:
            return cached
        return self._issue_token()

    def _issue_token(self) -> str:
        client_id = _required_env(self.config.client_id_env, "client_id_env")
        client_secret = _required_env(self.config.client_secret_env, "client_secret_env")
        body = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
        ).encode("utf-8")
        request = Request(
            f"{_base_url(self.config)}/oauth2/token",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with self.opener.open(request, timeout=self.config.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        token = str(payload["access_token"])
        self._save_cached_token(token, int(payload.get("expires_in") or 0))
        return token

    def _load_cached_token(self) -> str | None:
        if not self.config.token_cache_path:
            return None
        path = Path(self.config.token_cache_path)
        if not path.exists():
            return None
        payload = json.loads(path.read_text())
        expires_at = payload.get("expires_at")
        if not expires_at or expires_at <= utc_now().timestamp():
            return None
        return str(payload.get("access_token") or "") or None

    def _save_cached_token(self, token: str, expires_in: int) -> None:
        if not self.config.token_cache_path or expires_in <= 0:
            return
        path = Path(self.config.token_cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "access_token": token,
            "expires_at": utc_now().timestamp() + max(0, expires_in - 60),
        }
        path.write_text(json.dumps(payload, sort_keys=True))


def _required_env(env_name: str | None, label: str) -> str:
    if not env_name:
        raise ValueError(f"Toss {label} is required")
    value = os.getenv(env_name)
    if not value:
        raise ValueError(f"Toss environment variable is required: {env_name}")
    return value


def _base_url(config: BrokerAccountConfig) -> str:
    return (config.base_url or "https://openapi.tossinvest.com").rstrip("/")


__all__ = ["TossAuthManager"]
