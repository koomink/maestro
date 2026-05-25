import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path

from maestro.config.models import MaestroConfig
from maestro.config.strategy_account_mapping import prepare_config

STATE_IDENTITY_KEYS = (
    "mode",
    "profile_stage",
    "portfolio",
    "strategies",
    "universe",
    "datahub",
    "execution",
    "risk",
    "state",
    "audit",
    "kis",
    "reconciliation",
)


@dataclass(frozen=True)
class ConfigIdentity:
    path: str
    fingerprint: str
    state_fingerprint: str
    runtime_fingerprint: str

    def model_dump(self) -> dict[str, str]:
        return asdict(self)


def config_identity(path: str | Path) -> ConfigIdentity:
    config_path = Path(path).expanduser().resolve()
    prepared = prepare_config(config_path)
    config = MaestroConfig.model_validate(prepared.raw)
    canonical = config.model_dump(mode="json")
    canonical["profile_stage"] = config.profile_stage.value
    if config.state.identity_group:
        state_payload = {
            "state": canonical.get("state"),
            "state_identity_group": config.state.identity_group,
        }
    else:
        state_payload = {key: canonical.get(key) for key in STATE_IDENTITY_KEYS}
    return ConfigIdentity(
        path=str(config_path),
        fingerprint=sha256(prepared.fingerprint_bytes).hexdigest(),
        state_fingerprint=_fingerprint_payload(state_payload),
        runtime_fingerprint=_fingerprint_payload(canonical),
    )


def _fingerprint_payload(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


__all__ = ["ConfigIdentity", "config_identity"]
