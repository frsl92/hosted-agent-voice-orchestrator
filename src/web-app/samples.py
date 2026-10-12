"""Agents, interaction modes and sample conversations offered by the test app.

The sample conversations are shared by every agent: each line is something a customer would naturally say
to either design, so the same script can be used to compare them side by side.
"""

from __future__ import annotations

from typing import Any

AGENTS: list[dict[str, Any]] = [
    {
        "id": "handoff-agent",
        "kind": "handoff",
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
        "kind": "background",
        "name": "Background agent",
        "tagline": "Foreground agent confirms → background workers run",
        "description": (
            "A single foreground agent talks to the caller, proposes what it understood, and only after an explicit "
            "confirmation (judged by Decision-1) launches billing / order-status workers in the background and "
            "reports results as they arrive."
        ),
    },
]

MODES: list[dict[str, str]] = [
    {"id": "text", "name": "Text", "description": "Type your messages and read the replies."},
    {"id": "voice", "name": "Voice", "description": "Speak and listen. The transcript is shown for reference."},
]

SAMPLES: list[dict[str, Any]] = [
    {
        "id": "bill-and-order",
        "title": "Bill and order in one go",
        "description": "Two topics in one sentence: shows multi-topic detection, queueing and the hand-over between them.",
        "turns": [
            "Hi, I'd like to know about my latest bill and also check the status of order 12345.",
            "Yes, that's right, go ahead.",
            "Thanks, that's all I needed. Goodbye!",
        ],
    },
    {
        "id": "billing",
        "title": "Billing question",
        "description": "Balance and due date, then a clean end of call.",
        "turns": [
            "Hello, can you tell me how much I owe this month?",
            "Yes, that's right. And when is it due?",
            "That's all, thank you. Goodbye!",
        ],
    },
    {
        "id": "order-tracking",
        "title": "Order tracking",
        "description": "A shipped order with tracking details.",
        "turns": [
            "Hi, I'd like to check on my order 12345.",
            "Yes, that's right. What's the tracking number?",
            "Great, thanks. Bye!",
        ],
    },
    {
        "id": "order-processing",
        "title": "Order not shipped yet",
        "description": "The order is still processing: there is no tracking number yet, only an estimate.",
        "turns": ["Where is my order 67890?", "Yes, that's the one. When will it arrive?"],
    },
    {
        "id": "topic-switch",
        "title": "Change of topic",
        "description": "Starts with billing, then the customer switches to an order mid-conversation.",
        "turns": [
            "I'd like to know my current balance.",
            "Actually, forget that. What's going on with order 55555?",
            "Thanks, goodbye.",
        ],
    },
    {
        "id": "correction",
        "title": "Customer corrects themselves",
        "description": "The customer gives one order number and then corrects it.",
        "turns": ["Please check on order 12345.", "No, I meant order 67890.", "Yes, that's right. Thanks!"],
    },
    {
        "id": "missing-order",
        "title": "Missing order number",
        "description": "The agent has to ask for the order number before it can look anything up.",
        "turns": ["I want to know where my order is.", "It's 55555.", "Yes, that's right. Thank you!"],
    },
    {
        "id": "out-of-scope",
        "title": "Out-of-scope request",
        "description": "Something the agents cannot do, followed by an in-scope question.",
        "turns": ["Can you help me change my mobile plan?", "Okay, then can you check my previous bill?", "Yes, please."],
    },
]
