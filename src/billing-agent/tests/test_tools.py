import calltrace
import tools


async def _call(t, **kwargs):
    return await t.func(**kwargs) if hasattr(t, "func") else await t(**kwargs)


async def test_invoice_tool_returns_mock_data():
    assert "84.90" in await _call(tools.get_latest_invoice, period="current")


async def test_explain_charge_matches_keyword():
    assert "Roaming" in await _call(tools.explain_charge, charge="the roaming one")


async def test_return_to_router_is_registered():
    assert any(getattr(t, "name", "") == "return_to_router" for t in tools.BILLING_TOOLS)


def test_trace_events_are_json_lines(caplog):
    with caplog.at_level("INFO", logger="calltrace"):
        calltrace.emit("tool.call", name="x")
    assert any(r.getMessage().startswith("TRACE {") for r in caplog.records)
