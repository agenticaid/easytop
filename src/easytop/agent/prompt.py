"""System prompt: loaded verbatim from system_prompt.txt (extracted from vault Spec/06 Guardrails.md)."""

from pathlib import Path

SYSTEM_PROMPT = (
    (Path(__file__).parent / "system_prompt.txt").read_text(encoding="utf-8").rstrip("\n")
)
