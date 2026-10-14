"""Agent core: Claude tool-use loop over the family service tools. Channel-agnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import anthropic

from easytop.agent.guardrails import MAX_TOOL_CALLS, cap_answer, trim_tool_output
from easytop.agent.pricing import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER, PRICES_PER_MTOK
from easytop.agent.prompt import SYSTEM_PROMPT
from easytop.agent.tools import TOOLS, ReminderSetting, dispatch
from easytop.family.service import FamilyService
from easytop.files import strip_markers
from easytop.models import FileOffer

MODEL = "claude-sonnet-5-5"
RETRY_MESSAGE = (
    "משהו השתבש אצלי. נסו שוב בעוד דקה.\n\n"
    "Something went wrong on my side. Please try again in a minute."
)
EMPTY_MESSAGE = (
    "לא הצלחתי להרכיב תשובה. נסו לנסח אחרת.\n\n"
    "I couldn't put together an answer. Please try rephrasing."
)
TRUNCATED_MESSAGE = (
    "התשובה ארוכה מדי ונחתכה. שאלו משהו מצומצם יותר, למשל על ילד אחד או קטגוריה אחת.\n\n"
    "My answer got too long and was cut off. Please ask something narrower, "
    "for example one child or one category."
)
MAX_HISTORY_MESSAGES = 20
MAX_TOOL_LOG = 20
CACHEABLE = {"type": "ephemeral"}  # 5-minute prompt cache


@dataclass
class Conversation:
    """In-memory chat history (user text + final answers only). Nothing is persisted in stage 1."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    # One line per tool call made (time, name, arguments, ok/error; never content). The history above keeps
    # no tool traffic, so without this the model cannot tell what it already checked and "corrects" itself.
    tool_log: list[str] = field(default_factory=list)


@dataclass
class AgentResult:
    text: str
    tool_calls: list[str] = field(default_factory=list)
    # name + arguments of each call, for the analytics labels (category, kid scope); read in memory, never logged
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0  # input tokens written to the prompt cache (priced 1.25x)
    cache_read_tokens: int = (
        0  # input tokens read from the cache (priced 0.1x); not in input_tokens
    )
    cost_usd: float | None = None
    error: str | None = None
    hit_tool_limit: bool = False
    truncated: bool = False  # the model hit max_tokens before it finished
    # files of the messages the tool results carried, by the short ref the model cites in {{braces}} (Spec 14)
    offers: dict[str, tuple[FileOffer, ...]] = field(default_factory=dict)


class Agent:
    def __init__(
        self,
        client: Any,
        family: FamilyService,
        *,
        model: str = MODEL,
        clock=datetime.now,
        max_tool_calls: int = MAX_TOOL_CALLS,
        max_tokens: int = 8192,  # thinking tokens count against it; 4096 was reached by a 3-kid Hebrew answer
        prices_per_mtok: tuple[float, float] | None = None,
        reminders: ReminderSetting | None = None,
        effort: str | None = None,  # low | medium | high; None = the model's own default
    ):
        self._client = client
        self._family = family
        self._reminders = reminders
        self._model = model
        self._clock = clock
        self._max_calls = max_tool_calls
        self._max_tokens = max_tokens
        self._effort = effort
        # (input, output) USD per million tokens: given, else the published price of the model, else None
        self._prices = prices_per_mtok or PRICES_PER_MTOK.get(model)

    @property
    def model(self) -> str:
        return self._model

    def bind_reminders(self, reminders: ReminderSetting) -> None:
        """Tie the `set_reminder` tool to this parent's own reminder setting (the registry does it, like the
        family service is tied to the parent's session)."""
        self._reminders = reminders

    @property
    def family(self) -> FamilyService:
        """The parent's family service (the nudge asks it whether there is school tomorrow)."""
        return self._family

    def _system(self, context: str, tool_log: list[str]) -> list[dict[str, str]]:
        now = self._clock()
        blocks = [
            # the fixed prompt is cacheable (with the tool list before it); the lines below change per call
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": CACHEABLE},
            {
                "type": "text",
                "text": f"Current local date and time: {now:%A %Y-%m-%d %H:%M} (Israel).",
            },
        ]
        if context:
            blocks.append({"type": "text", "text": f"CONTEXT (from memory)\n{context}"})
        if tool_log:
            blocks.append(
                {
                    "type": "text",
                    "text": "TOOL CALLS ALREADY MADE IN THIS CONVERSATION (a system record, not your "
                    "memory; arguments and outcome only, no content):\n" + "\n".join(tool_log),
                }
            )
        return blocks

    async def _create(
        self, messages: list[dict[str, Any]], system: list[dict[str, str]], no_tools: bool
    ):
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "tools": [*TOOLS[:-1], {**TOOLS[-1], "cache_control": CACHEABLE}],
            "messages": messages,
        }
        if self._effort:
            kwargs["output_config"] = {"effort": self._effort}
        if no_tools:
            kwargs["tool_choice"] = {"type": "none"}
        last: Exception | None = None
        for _ in range(2):  # one retry
            try:
                return await self._client.messages.create(**kwargs)
            except anthropic.APIError as e:
                last = e
        raise last  # type: ignore[misc]

    async def ask(self, conversation: Conversation, text: str, *, context: str = "") -> AgentResult:
        result = AgentResult(text="")
        messages = [*conversation.messages, {"role": "user", "content": text}]
        system = self._system(context, conversation.tool_log)
        calls = 0
        try:
            for _ in range(self._max_calls + 2):
                resp = await self._create(messages, system, no_tools=calls >= self._max_calls)
                result.input_tokens += resp.usage.input_tokens
                result.output_tokens += resp.usage.output_tokens
                result.cache_write_tokens += (
                    getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
                )
                result.cache_read_tokens += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
                tool_uses = [b for b in resp.content if b.type == "tool_use"]
                if resp.stop_reason != "tool_use" or not tool_uses:
                    result.text = "".join(b.text for b in resp.content if b.type == "text").strip()
                    result.truncated = resp.stop_reason == "max_tokens"
                    break
                messages.append({"role": "assistant", "content": [_block(b) for b in resp.content]})
                outputs = []
                for b in tool_uses:
                    if calls >= self._max_calls:
                        result.hit_tool_limit = True
                        out, is_err = (
                            "Tool call limit reached. Answer with what you already have.",
                            True,
                        )
                    else:
                        calls += 1
                        result.tool_calls.append(b.name)
                        result.tool_uses.append({"name": b.name, "input": dict(b.input or {})})
                        data = await dispatch(
                            self._family, b.name, b.input, reminders=self._reminders
                        )
                        for ref, files in (data.get("_offers") or {}).items():
                            result.offers[ref] = tuple(FileOffer(**f) for f in files)
                        out, is_err = trim_tool_output(data), "error" in data
                        args = ", ".join(f"{k}={v}" for k, v in dict(b.input).items())
                        conversation.tool_log = [
                            *conversation.tool_log,
                            f"{self._clock():%H:%M} {b.name}({args}) -> {'error' if is_err else 'ok'}",
                        ][-MAX_TOOL_LOG:]
                    outputs.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": b.id,
                            "content": out,
                            "is_error": is_err,
                        }
                    )
                messages.append({"role": "user", "content": outputs})
        except anthropic.APIError as e:
            result.error = f"{type(e).__name__}"
            result.text = RETRY_MESSAGE
            return self._finish(result)
        if result.truncated and not result.text:
            result.text = TRUNCATED_MESSAGE  # a failed turn: not remembered
            return self._finish(result)
        result.text = cap_answer(result.text) if result.text else EMPTY_MESSAGE
        conversation.messages = [
            *conversation.messages,
            {"role": "user", "content": text},
            {"role": "assistant", "content": strip_markers(result.text)},
        ][-MAX_HISTORY_MESSAGES:]
        return self._finish(result)

    def _finish(self, r: AgentResult) -> AgentResult:
        if self._prices:
            p_in, p_out = self._prices
            r.cost_usd = (
                r.input_tokens * p_in
                + r.cache_write_tokens * p_in * CACHE_WRITE_MULTIPLIER
                + r.cache_read_tokens * p_in * CACHE_READ_MULTIPLIER
                + r.output_tokens * p_out
            ) / 1e6
        return r


def _block(b: Any) -> dict[str, Any]:
    """An assistant content block as it must be sent back with tool results. Thinking blocks go back
    unchanged (the API checks their signature); an unknown block type is passed through as it came."""
    match b.type:
        case "tool_use":
            return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
        case "text":
            return {"type": "text", "text": b.text}
        case "thinking":
            return {"type": "thinking", "thinking": b.thinking, "signature": b.signature}
        case "redacted_thinking":
            return {"type": "redacted_thinking", "data": b.data}
    return b.model_dump(exclude_none=True)  # a block type we do not know: let the API judge it
