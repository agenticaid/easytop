"""Scripted stand-in for the Anthropic client (no network, no cost)."""

from types import SimpleNamespace as NS


def text(t):
    return NS(type="text", text=t)


def thinking(t, signature="sig-1"):
    return NS(type="thinking", thinking=t, signature=signature)


def redacted_thinking(data="blob"):
    return NS(type="redacted_thinking", data=data)


def tool_use(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def response(*blocks, stop=None, in_tok=10, out_tok=5, cache_write=None, cache_read=None):
    stop = stop or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    usage = NS(input_tokens=in_tok, output_tokens=out_tok)
    if cache_write is not None:  # the real API reports these only when caching is in play
        usage.cache_creation_input_tokens = cache_write
    if cache_read is not None:
        usage.cache_read_input_tokens = cache_read
    return NS(content=list(blocks), stop_reason=stop, usage=usage)


class ScriptedClient:
    """messages.create returns queued responses (or raises queued exceptions); records every call."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.messages = NS(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
