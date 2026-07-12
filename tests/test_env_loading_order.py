import os

from maestro.config.env import load_default_env_files


def test_default_env_loader_prefers_operator_env_over_project_dotenv(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("KEY", raising=False)
    monkeypatch.delenv("EXTRA", raising=False)
    operator_env = tmp_path / "maestro.env"
    operator_env.write_text("KEY=operator\n", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=dotenv\nEXTRA=dev\n", encoding="utf-8")

    load_default_env_files(cwd=tmp_path, operator_env_path=operator_env)

    assert os.environ["KEY"] == "operator"
    assert os.environ["EXTRA"] == "dev"


def test_default_env_loader_uses_project_dotenv_when_operator_env_missing(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("KEY", raising=False)
    (tmp_path / ".env").write_text("KEY=dotenv\n", encoding="utf-8")

    load_default_env_files(cwd=tmp_path, operator_env_path=tmp_path / "missing.env")

    assert os.environ["KEY"] == "dotenv"


def test_default_env_loader_does_not_override_existing_process_env(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("KEY", "process")
    operator_env = tmp_path / "maestro.env"
    operator_env.write_text("KEY=operator\n", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=dotenv\n", encoding="utf-8")

    load_default_env_files(cwd=tmp_path, operator_env_path=operator_env)

    assert os.environ["KEY"] == "process"
