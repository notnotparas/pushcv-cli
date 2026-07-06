"""CLI-level tests for pipeline state transitions.

Runs commands through Typer's CliRunner against a throwaway workspace
(temporary cwd + temporary SQLite engine). No network: the AI engine is
monkeypatched where a command would call it.
"""
import pytest
from sqlmodel import Session, select
from typer.testing import CliRunner

from pushcv import main
from pushcv.core import Workspace
from pushcv.models import JobApplication

runner = CliRunner()


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Isolated workspace: temp cwd, temp Workspace, prompts pre-answered."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "ws", Workspace(tmp_path))
    # Pre-answer the one-time AI-salary prompt so commands run unattended.
    (tmp_path / ".pushcv.json").write_text('{"ai_salary_enabled": false}\n')
    return tmp_path


def _status_of_first_job() -> str:
    with Session(main.ws.engine) as session:
        job = session.exec(select(JobApplication)).one()
        return job.status


def test_draft_advances_a_drafting_job(workspace, monkeypatch):
    monkeypatch.setattr(main, "generate_tailored_resume", lambda **kw: "# Resume")
    (workspace / "profile.md").write_text("# pushcv Profile\nAlex, engineer.")

    assert runner.invoke(main.app, ["add", "Acme", "Engineer"]).exit_code == 0
    result = runner.invoke(main.app, ["draft", "1"])
    assert result.exit_code == 0
    assert _status_of_first_job() == "ready_to_apply"


def test_draft_does_not_demote_an_interviewing_job(workspace, monkeypatch):
    # Regression: re-drafting a resume for a job already past Drafting used to
    # yank it back to the Drafting column.
    monkeypatch.setattr(main, "generate_tailored_resume", lambda **kw: "# Resume")
    (workspace / "profile.md").write_text("# pushcv Profile\nAlex, engineer.")

    runner.invoke(main.app, ["add", "Acme", "Engineer"])
    assert runner.invoke(main.app, ["move", "1", "interviewing"]).exit_code == 0
    result = runner.invoke(main.app, ["draft", "1"])
    assert result.exit_code == 0
    assert _status_of_first_job() == "interviewing"


def test_note_counts_entries_not_lines(workspace):
    # Regression: a note whose text contains a newline used to inflate the count.
    runner.invoke(main.app, ["add", "Acme", "Engineer"])
    result = runner.invoke(main.app, ["note", "1", "first line\nsecond line"])
    assert result.exit_code == 0
    assert "1 note(s)" in result.output


def test_workspace_dotenv_overrides_ai_settings(tmp_path, monkeypatch):
    """Regression for #9: a .env in the *working directory* must override the
    AI server address and model for installed users — no-arg load_dotenv()
    searches from the package directory and never found it."""
    import importlib
    import os

    from pushcv import ai_engine

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "PUSHCV_AI_BASE=http://localhost:9999/v1\nPUSHCV_AI_MODEL=test-model\n"
    )
    try:
        importlib.reload(ai_engine)
        assert ai_engine.LOCAL_API_BASE == "http://localhost:9999/v1"
        importlib.reload(main)
        assert main.DEFAULT_AI_MODEL == "test-model"
    finally:
        # load_dotenv mutates os.environ for the whole process — scrub and
        # restore both modules so later tests see the defaults.
        for key in ("PUSHCV_AI_BASE", "PUSHCV_AI_KEY", "PUSHCV_AI_MODEL"):
            os.environ.pop(key, None)
        importlib.reload(ai_engine)
        importlib.reload(main)


def test_move_rejects_unknown_status(workspace):
    runner.invoke(main.app, ["add", "Acme", "Engineer"])
    result = runner.invoke(main.app, ["move", "1", "definitely-not-a-status"])
    assert result.exit_code == 1
    assert _status_of_first_job() == "drafting"
