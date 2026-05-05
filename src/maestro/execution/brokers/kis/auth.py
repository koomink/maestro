import os

from maestro.config.models import KISConfig


class KISAuthManager:
    def __init__(self, config: KISConfig) -> None:
        self.config = config

    def validate_readonly_credentials(self) -> None:
        if self.config.provider == "mock":
            return
        missing = [
            env_name
            for env_name in [
                self.config.app_key_env,
                self.config.app_secret_env,
                self.config.access_token_env,
            ]
            if not os.getenv(env_name)
        ]
        if missing:
            raise ValueError(f"Missing KIS credential environment variables: {missing}")
