from types import SimpleNamespace

import runner as runner_mod
from orchestrator import ROUTER, TurnSignals


def upd(text="", kinds=()):
    return SimpleNamespace(text=text, contents=[SimpleNamespace(type=k) for k in kinds] or [SimpleNamespace(type="text")])


class FakeMafAgent:
    def __init__(self, updates, signals, raise_after=None):
        self._updates, self._signals = updates, signals

    def run(self, messages, stream=False):
        async def gen():
            for u in self._updates:
                if u == "RETURN":
                    self._signals.return_requested = True
                    continue
                yield u

        return gen()


async def collect(updates):
    signals = TurnSignals()
    r = runner_mod.MafAgentRunner(client=None)
    runner_mod._FACTORIES[ROUTER] = lambda client, directive, sig: FakeMafAgent(updates, signals)
    return "".join([c async for c in r.stream(ROUTER, [{"role": "user", "text": "hi"}], None, signals)])


async def test_streams_live_and_spaces_after_tool_call():
    out = await collect(
        [upd("Let me check."), upd("", ["function_call"]), upd("", ["function_result"]), upd("It shipped.")]
    )
    assert out == "Let me check. It shipped."


async def test_repeat_after_return_to_router_is_dropped():
    out = await collect(
        [
            upd("Your order is processing."),
            upd("", ["function_call"]),
            "RETURN",
            upd("", ["function_result"]),
            upd("Your order is processing."),
        ]
    )
    assert out == "Your order is processing."


async def test_new_text_after_return_to_router_is_kept():
    out = await collect(
        [upd("", ["function_call"]), "RETURN", upd("", ["function_result"]), upd("It shipped yesterday.")]
    )
    assert out == "It shipped yesterday."
