from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol

from telepath.llm.base import (
    LLMFailure,
    LLMUnavailable,
    format_single_prompt,
)


class CopilotUnavailable(LLMUnavailable):
    pass


class CopilotError(LLMFailure):
    pass


class CommandRunner(Protocol):
    def run(self, args: list[str], *, timeout: int) -> str: ...


class SubprocessRunner:
    def run(self, args: list[str], *, timeout: int) -> str:
        try:
            completed = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CopilotError(f"Copilot CLI timed out after {timeout} seconds") from exc
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or "unknown Copilot CLI error"
            raise CopilotError(stderr)
        return completed.stdout


@dataclass
class CopilotCliTextPolisher:
    runner: CommandRunner | None = None
    command: str = "copilot"
    model: str | None = None
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if self.runner is None:
            self.runner = SubprocessRunner()

    def polish(self, text: str, prompt: str | None = None) -> str:
        full_prompt = format_single_prompt(text, prompt)
        args = [self.command, "-p", full_prompt, "-s", "--no-ask-user"]
        if self.model:
            args.extend(["--model", self.model])
        try:
            assert self.runner is not None
            output = self.runner.run(args, timeout=self.timeout_seconds)
        except FileNotFoundError as exc:
            raise CopilotUnavailable(
                f"Copilot CLI was not found: {self.command}. Install and authenticate GitHub Copilot CLI first."
            ) from exc
        return output.strip()
