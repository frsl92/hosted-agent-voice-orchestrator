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
            "A router greets the caller. Microsoft-Decision-1 detects the topic(s) in each utterance and the router "
            "reads them back for confirmation. Once the customer confirms, the call is handed to the billing and/or "
            "order-status sub-agent in turn; each explains its result and hands back, and the router closes the call."
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
        "description": (
            "Two topics in one sentence. The agent reads both back, you confirm, each topic is then looked up "
            "and explained in turn, and you close the call."
        ),
        "turns": [
            "Olá, queria saber o valor da minha última fatura e também ver o estado da encomenda 12345.",
            "Sim, está correto, pode avançar.",
            "Obrigado, era só isso que precisava. Até logo!",
        ],
    },
    {
        "id": "billing",
        "title": "Billing question",
        "description": "One topic: request, confirmation, the invoice details, then a clean end of call.",
        "turns": [
            "Bom dia, pode dizer-me quanto tenho para pagar este mês?",
            "Sim, é isso mesmo, pode verificar.",
            "Era só isso, obrigado. Até logo!",
        ],
    },
    {
        "id": "order-tracking",
        "title": "Order tracking",
        "description": "A shipped order: the agent confirms the order, then gives status, estimate and tracking details.",
        "turns": [
            "Olá, queria saber da minha encomenda 12345.",
            "Sim, é essa, pode verificar.",
            "Ótimo, obrigado. Até logo!",
        ],
    },
    {
        "id": "order-processing",
        "title": "Order not shipped yet",
        "description": "The order is still processing: there is no tracking number yet, only an estimate.",
        "turns": [
            "Onde está a minha encomenda 67890?",
            "Sim, é essa, pode verificar.",
            "Percebi, obrigado. Até logo!",
        ],
    },
    {
        "id": "two-orders",
        "title": "Two orders, one after the other",
        "description": (
            "Asks about order 12345 and, once the answer is given, about order 67890. With the remote sub-agents, "
            "the second lookup reuses the same Foundry session of the order sub-agent."
        ),
        "turns": [
            "Olá, queria saber o estado da encomenda 12345.",
            "Sim, é essa, pode verificar.",
            "Obrigado. Já agora, pode ver-me também a encomenda 67890?",
            "Sim, é essa, pode verificar.",
            "Perfeito, era só isso. Até logo!",
        ],
    },
    {
        "id": "topic-switch",
        "title": "Change of topic",
        "description": (
            "The customer asks for billing, rejects the read-back and switches to an order. The agent proposes "
            "the new topic, waits for the confirmation and only then looks it up."
        ),
        "turns": [
            "Gostava de saber o meu saldo atual.",
            "Na verdade, esqueça isso. O que se passa com a encomenda 55555?",
            "Sim, pode verificar.",
            "Obrigado, até logo.",
        ],
    },
    {
        "id": "correction",
        "title": "Customer corrects themselves",
        "description": "The customer gives one order number, rejects the read-back to correct it, then confirms the new one.",
        "turns": [
            "Por favor, veja-me a encomenda 12345.",
            "Não, eu queria dizer a encomenda 67890.",
            "Sim, está certo, pode verificar.",
            "Obrigado, até logo!",
        ],
    },
    {
        "id": "missing-order",
        "title": "Missing order number",
        "description": "The agent asks for the order number while confirming, and only looks it up once it has it.",
        "turns": [
            "Quero saber onde está a minha encomenda.",
            "Sim, é a 55555, pode verificar.",
            "Muito obrigado, até logo!",
        ],
    },
    {
        "id": "out-of-scope",
        "title": "Out-of-scope request",
        "description": "Something the agents cannot do, followed by an in-scope question that goes through the normal confirmation.",
        "turns": [
            "Pode ajudar-me a mudar de tarifário móvel?",
            "Está bem, então pode ver-me a fatura anterior?",
            "Sim, por favor.",
            "Era só isso, obrigado. Até logo!",
        ],
    },
]
