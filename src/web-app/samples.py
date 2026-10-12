"""Agents, interaction modes and sample conversations offered by the test app.

The sample conversations are shared by every agent: each line is something a customer would naturally say
to either design, so the same script can be used to compare them side by side. The lines are written in
European Portuguese, the main language of the callers; the agents also follow the caller into English.
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
            "Olá, queria saber o valor da minha última fatura e também ver o estado da encomenda 12345.",
            "Sim, está correto, pode avançar.",
            "Obrigado, era só isso que precisava. Até logo!",
        ],
    },
    {
        "id": "billing",
        "title": "Billing question",
        "description": "Balance and due date, then a clean end of call.",
        "turns": [
            "Bom dia, pode dizer-me quanto tenho para pagar este mês?",
            "Sim, é isso mesmo. E até quando tenho de pagar?",
            "Era só isso, obrigado. Até logo!",
        ],
    },
    {
        "id": "order-tracking",
        "title": "Order tracking",
        "description": "A shipped order with tracking details.",
        "turns": [
            "Olá, queria saber da minha encomenda 12345.",
            "Sim, é essa. Qual é o número de seguimento?",
            "Ótimo, obrigado. Até logo!",
        ],
    },
    {
        "id": "order-processing",
        "title": "Order not shipped yet",
        "description": "The order is still processing: there is no tracking number yet, only an estimate.",
        "turns": ["Onde está a minha encomenda 67890?", "Sim, é essa. Quando é que vai chegar?"],
    },
    {
        "id": "topic-switch",
        "title": "Change of topic",
        "description": "Starts with billing, then the customer switches to an order mid-conversation.",
        "turns": [
            "Gostava de saber o meu saldo atual.",
            "Na verdade, esqueça isso. O que se passa com a encomenda 55555?",
            "Obrigado, até logo.",
        ],
    },
    {
        "id": "correction",
        "title": "Customer corrects themselves",
        "description": "The customer gives one order number and then corrects it.",
        "turns": ["Por favor, veja-me a encomenda 12345.", "Não, eu queria dizer a encomenda 67890.", "Sim, está certo. Obrigado!"],
    },
    {
        "id": "missing-order",
        "title": "Missing order number",
        "description": "The agent has to ask for the order number before it can look anything up.",
        "turns": ["Quero saber onde está a minha encomenda.", "É a 55555.", "Sim, está certo. Muito obrigado!"],
    },
    {
        "id": "out-of-scope",
        "title": "Out-of-scope request",
        "description": "Something the agents cannot do, followed by an in-scope question.",
        "turns": ["Pode ajudar-me a mudar de tarifário móvel?", "Está bem, então pode ver-me a fatura anterior?", "Sim, por favor."],
    },
]
