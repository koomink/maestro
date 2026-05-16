from pathlib import Path

from dotenv import load_dotenv


def load_project_dotenv(cwd: Path | None = None) -> None:
    load_dotenv(dotenv_path=(cwd or Path.cwd()) / ".env", override=False)


__all__ = ["load_project_dotenv"]
