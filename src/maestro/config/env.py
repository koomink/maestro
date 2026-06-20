import os
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_OPERATOR_ENV_FILE = Path("/etc/maestro/maestro.env")
OPERATOR_ENV_FILE_ENV_VAR = "MAESTRO_ENV_FILE"


def load_project_dotenv(cwd: Path | None = None) -> None:
    load_dotenv(dotenv_path=(cwd or Path.cwd()) / ".env", override=False)


def load_env_file(path: str | Path) -> bool:
    return load_dotenv(dotenv_path=Path(path), override=False)


def load_default_env_files(
    cwd: Path | None = None,
    operator_env_path: str | Path | None = None,
) -> None:
    load_project_dotenv(cwd)
    resolved_operator_env = Path(
        operator_env_path
        or os.getenv(OPERATOR_ENV_FILE_ENV_VAR)
        or DEFAULT_OPERATOR_ENV_FILE
    )
    if resolved_operator_env.exists():
        load_env_file(resolved_operator_env)


__all__ = [
    "DEFAULT_OPERATOR_ENV_FILE",
    "OPERATOR_ENV_FILE_ENV_VAR",
    "load_default_env_files",
    "load_env_file",
    "load_project_dotenv",
]
