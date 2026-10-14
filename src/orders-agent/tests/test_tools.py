import calltrace
import tools


async def _call(t, **kwargs):
    return await t.func(**kwargs) if hasattr(t, "func") else await t(**kwargs)


async def test_lookup_known_order():
    assert "Fiber router" in await _call(tools.lookup_order, order_id="12345")


async def test_second_order_in_same_session_is_independent():
    assert "5G smartphone" in await _call(tools.lookup_order, order_id="67890")


async def test_unknown_order():
    assert "No order found" in await _call(tools.get_delivery_eta, order_id="00000")


async def test_return_to_router_is_registered():
    assert any(getattr(t, "name", "") == "return_to_router" for t in tools.ORDER_TOOLS)


def test_trace_events_are_json_lines(caplog):
    with caplog.at_level("INFO", logger="calltrace"):
        calltrace.emit("tool.call", name="x")
    assert any(r.getMessage().startswith("TRACE {") for r in caplog.records)
