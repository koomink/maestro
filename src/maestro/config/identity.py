from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path


@dataclass(frozen=True)
class ConfigIdentity:
    path: str
    fingerprint: str

    def model_dump(self) -> dict[str, str]:
        return asdict(self)


def config_identity(path: str | Path) -> ConfigIdentity:
    config_path = Path(path).expanduser().resolve()
    return ConfigIdentity(
        path=str(config_path),
        fingerprint=sha256(config_path.read_bytes()).hexdigest(),
    )


__all__ = ["ConfigIdentity", "config_identity"]
