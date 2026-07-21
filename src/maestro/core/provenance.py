from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path


@dataclass(frozen=True)
class DeploymentIdentity:
    commit: str | None
    source_fingerprint: str
    dirty: bool | None

    def model_dump(self) -> dict[str, str | bool | None]:
        return asdict(self)


@lru_cache(maxsize=1)
def current_deployment_identity() -> DeploymentIdentity:
    return deployment_identity(Path(__file__).resolve())


def deployment_identity(start_path: str | Path) -> DeploymentIdentity:
    override = os.environ.get("MAESTRO_DEPLOYMENT_COMMIT")
    repo = _find_git_root(Path(start_path).expanduser().resolve())
    if repo is None:
        value = override or "git-unavailable"
        return DeploymentIdentity(
            commit=override,
            source_fingerprint=sha256(value.encode("utf-8")).hexdigest(),
            dirty=None,
        )

    commit = override or _git(repo, "rev-parse", "HEAD").decode().strip()
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    dirty = bool(status.strip())
    digest = sha256()
    digest.update(commit.encode("utf-8"))
    digest.update(b"\0")
    if dirty:
        digest.update(_git(repo, "diff", "--binary", "HEAD", "--"))
        untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
        for raw_path in sorted(path for path in untracked.split(b"\0") if path):
            digest.update(raw_path)
            digest.update(b"\0")
            digest.update((repo / os.fsdecode(raw_path)).read_bytes())
            digest.update(b"\0")
    return DeploymentIdentity(commit=commit, source_fingerprint=digest.hexdigest(), dirty=dirty)


def _find_git_root(start_path: Path) -> Path | None:
    current = start_path if start_path.is_dir() else start_path.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    ).stdout


__all__ = ["DeploymentIdentity", "current_deployment_identity", "deployment_identity"]
