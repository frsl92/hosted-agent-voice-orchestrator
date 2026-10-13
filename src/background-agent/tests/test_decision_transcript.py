from decision import LATEST_MARKER, format_transcript

TURNS = [
    ("user", "my bill and order 12345"),
    ("assistant", "Done, here is the bill."),
    ("user", "thanks, that's all"),
]


def test_latest_customer_message_is_marked():
    lines = format_transcript(TURNS).split("\n")
    assert lines == [
        "Customer: my bill and order 12345",
        "Agent: Done, here is the bill.",
        LATEST_MARKER,
        "Customer: thanks, that's all",
    ]


def test_single_message_needs_no_marker():
    assert format_transcript(TURNS[:1]) == "Customer: my bill and order 12345"


def test_context_is_limited_but_latest_always_kept():
    turns = [("user", f"q{i}") if i % 2 == 0 else ("assistant", f"a{i}") for i in range(20)] + [("user", "last")]
    out = format_transcript(turns).split("\n")
    assert len(out) == 6 + 1 and out[-1] == "Customer: last" and out[-2] == LATEST_MARKER
