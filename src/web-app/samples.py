"""Agents and sample conversations offered by the test app."""

from __future__ import annotations

from typing import Any

AGENTS: list[dict[str, Any]] = [
    {
        "id": "handoff-agent",
        "name": "Handoff agent",
        "tagline": "Router → billing / orders specialist → router",
        "description": (
            "A router greets the caller. Microsoft-Decision-1 detects the topic(s) in each utterance and the call is "
            "handed to the billing or order-status sub-agent, which talks to the customer until the topic is resolved "
            "and then hands back to the router (or on to the next queued topic)."
        ),
    },
    {
        "id": "background-agent",
        "name": "Background agent",
        "tagline": "Foreground agent confirms → background workers run",
        "description": (
            "A single foreground agent talks to the caller, proposes what it understood, and only after an explicit "
            "confirmation (judged by Decision-1) launches billing / order-status workers in the background and "
            "reports results as they arrive."
        ),
    },
]

SAMPLES: dict[str, list[dict[str, Any]]] = {
    "handoff-agent": [
        {
            "id": "two-topics",
            "title": "Two topics in one sentence",
            "description": "Roaming charge + order status. Shows multi-topic detection, queueing and the bridge between sub-agents.",
            "turns": [
                "Hi, I don't understand the roaming charge on my bill, and I'd also like to know where order 12345 is.",
                "Okay, thanks. That explains it.",
                "No, that's everything. Goodbye!",
            ],
        },
        {
            "id": "billing",
            "title": "Billing question",
            "description": "Balance and due date, then a clean call end.",
            "turns": [
                "Hello, can you tell me how much I owe this month?",
                "And when is it due?",
                "That's all, thank you.",
            ],
        },
        {
            "id": "order-tracking",
            "title": "Order tracking",
            "description": "Shipped order with tracking details.",
            "turns": [
                "Hi, I'd like to check on my order 12345.",
                "What's the tracking number?",
                "Great, thanks. Bye!",
            ],
        },
        {
            "id": "order-processing",
            "title": "Order not shipped yet",
            "description": "Order still processing: no tracking number, only an ETA.",
            "turns": ["Where is my order 67890?", "When will it arrive?"],
        },
        {
            "id": "topic-switch",
            "title": "Topic switch mid-conversation",
            "description": "Starts with billing, then raises an order while the billing agent is active.",
            "turns": [
                "I'd like to know my current balance.",
                "Actually, can you also tell me what's going on with order 55555?",
                "Thanks, goodbye.",
            ],
        },
        {
            "id": "out-of-scope",
            "title": "Out of scope request",
            "description": "Something the router cannot help with, followed by an in-scope question.",
            "turns": ["Can you help me change my mobile plan?", "Okay, then what about my last invoice?"],
        },
    ],
    "background-agent": [
        {
            "id": "bill-and-order",
            "title": "Bill + order with confirmation",
            "description": "Two requests proposed together, confirmed, then run as background tasks.",
            "turns": [
                "Hi, I'd like to know about my latest bill and also check the status of order 12345.",
                "Yes, that's right.",
                "No, nothing else. Goodbye!",
            ],
        },
        {
            "id": "previous-bill",
            "title": "Previous bill",
            "description": "A single billing request for the previous invoice period.",
            "turns": ["Can you check my previous bill?", "Yes please.", "That's all, thank you."],
        },
        {
            "id": "correction",
            "title": "Customer corrects the plan",
            "description": "The customer says 'no' to the proposal and corrects the order number.",
            "turns": ["Please check on order 12345.", "No, I meant order 67890.", "Yes, go ahead."],
        },
        {
            "id": "missing-order",
            "title": "Missing order number",
            "description": "The agent has to ask for the order number before it can propose anything.",
            "turns": ["I want to know where my order is.", "It's 55555.", "Yes, go ahead."],
        },
        {
            "id": "cancel",
            "title": "Cancel while working",
            "description": "Cancel the request after it has been confirmed.",
            "turns": ["Check the status of order 12345 please.", "Yes.", "Actually, never mind. Cancel that."],
        },
        {
            "id": "out-of-scope",
            "title": "Out of scope request",
            "description": "Something the agent cannot do, then an in-scope question.",
            "turns": ["Can you upgrade my data plan?", "Okay, what about my current bill then?"],
        },
    ],
}
