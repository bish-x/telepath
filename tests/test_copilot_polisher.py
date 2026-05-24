import pytest

from telepath.llm.copilot_cli import CopilotCliTextPolisher, CopilotUnavailable
from telepath.prompts import DEFAULT_TEXT_POLISH_PROMPT


class RecordingRunner:
    def __init__(self, stdout="polished text", error=None):
        self.stdout = stdout
        self.error = error
        self.calls = []

    def run(self, args, *, timeout):
        self.calls.append((args, timeout))
        if self.error:
            raise self.error
        return self.stdout


def test_copilot_polisher_uses_non_interactive_cli_with_strict_prompt():
    runner = RecordingRunner(stdout="Привет, мир.\n\nКак дела?")
    polisher = CopilotCliTextPolisher(runner=runner, command="copilot", model="gpt-5.2", timeout_seconds=30)

    result = polisher.polish("привет мир как дела")

    assert result == "Привет, мир.\n\nКак дела?"
    args, timeout = runner.calls[0]
    assert args[:2] == ["copilot", "-p"]
    assert "-s" in args
    assert "--no-ask-user" in args
    assert "--model" in args
    assert "gpt-5.2" in args
    assert "Не делай summary" in args[2]
    assert "привет мир как дела" in args[2]
    assert timeout == 30


def test_default_prompt_cleans_voice_transcripts_without_changing_meaning():
    prompt = DEFAULT_TEXT_POLISH_PROMPT

    assert "Не делай summary" in prompt
    assert "не добавляй новых фактов" in prompt.lower()
    assert "слова-паразиты" in prompt
    assert "очевидные ошибки распознавания" in prompt
    assert "термины" in prompt
    assert "мат" in prompt
    assert "смысловые абзацы" in prompt
    assert "Не дроби текст" in prompt
    assert "Новый абзац ставь только" in prompt
    assert "Не начинай новый абзац" in prompt
    assert "Для короткого голосового" in prompt
    assert "2-3 абзаца" in prompt
    assert "Верни только готовый текст" in prompt


def test_copilot_polisher_accepts_runtime_prompt_override():
    runner = RecordingRunner(stdout="Готовый текст.")
    polisher = CopilotCliTextPolisher(runner=runner)

    polisher.polish("сырой текст", prompt="Мой prompt: только пунктуация.")

    args, _ = runner.calls[0]
    assert "Мой prompt: только пунктуация." in args[2]
    assert "сырой текст" in args[2]


def test_copilot_polisher_fails_clearly_when_cli_is_missing():
    runner = RecordingRunner(error=FileNotFoundError("copilot"))
    polisher = CopilotCliTextPolisher(runner=runner)

    with pytest.raises(CopilotUnavailable, match="Copilot CLI was not found"):
        polisher.polish("text")


def test_copilot_polisher_omits_model_flag_when_unset():
    runner = RecordingRunner(stdout="ok")
    polisher = CopilotCliTextPolisher(runner=runner, command="copilot", model=None)

    polisher.polish("text")

    args, _ = runner.calls[0]
    assert "--model" not in args


# --- SubprocessRunner ------------------------------------------------------
# Exercises the real subprocess-backed runner with monkeypatched subprocess.run.

class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_subprocess_runner_returns_stdout_on_zero_exit(monkeypatch):
    import subprocess
    from telepath.llm.copilot_cli import SubprocessRunner

    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["timeout"] = kwargs.get("timeout")
        return _FakeCompleted(returncode=0, stdout="polished output\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    out = SubprocessRunner().run(["copilot", "-p", "x"], timeout=30)

    assert out == "polished output\n"
    assert captured["args"] == ["copilot", "-p", "x"]
    assert captured["timeout"] == 30


def test_subprocess_runner_raises_copilot_error_on_nonzero_exit(monkeypatch):
    import subprocess
    from telepath.llm.copilot_cli import CopilotError, SubprocessRunner

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kw: _FakeCompleted(returncode=2, stdout="", stderr="boom"),
    )

    with pytest.raises(CopilotError, match="boom"):
        SubprocessRunner().run(["copilot"], timeout=10)


def test_subprocess_runner_wraps_timeout_as_copilot_error(monkeypatch):
    import subprocess
    from telepath.llm.copilot_cli import CopilotError, SubprocessRunner

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CopilotError, match="timed out after 7 seconds"):
        SubprocessRunner().run(["copilot"], timeout=7)


def test_subprocess_runner_provides_generic_error_when_stderr_empty(monkeypatch):
    import subprocess
    from telepath.llm.copilot_cli import CopilotError, SubprocessRunner

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kw: _FakeCompleted(returncode=2, stdout="", stderr=""),
    )

    with pytest.raises(CopilotError, match="unknown Copilot CLI error"):
        SubprocessRunner().run(["copilot"], timeout=10)


def test_copilot_polisher_default_runner_is_subprocess(monkeypatch):
    import subprocess
    from telepath.llm.copilot_cli import CopilotCliTextPolisher

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kw: _FakeCompleted(returncode=0, stdout="hello\n"),
    )

    polisher = CopilotCliTextPolisher(command="copilot")
    assert polisher.runner is not None
    assert polisher.polish("text") == "hello"
