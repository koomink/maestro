import pytest


@pytest.fixture(autouse=True)
def _isolate_default_operator_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("MAESTRO_ENV_FILE", str(tmp_path / "missing_maestro.env"))
