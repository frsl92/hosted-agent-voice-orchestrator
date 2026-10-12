"""Tiny language helper: the call defaults to European Portuguese, English is the second language.

Only used for the fixed (non-LLM) sentences; everything the LLM says follows the customer's language
through the prompts.
"""

from __future__ import annotations

import re

PT, EN = "pt", "en"
DEFAULT = PT

CALL_START = "[call connected]"

_PT_WORDS = frozenset(
    "o a os as um uma uns umas de do da dos das no na nos nas que eu meu minha meus minhas fatura faturas "
    "encomenda encomendas por favor obrigado obrigada olá ola bom boa dia tarde noite quero queria gostava saber "
    "sim não nao estado está esta onde quando quanto posso pode podem adeus tudo com para em é sobre tenho "
    "preciso ainda já mas se foi mensalidade pagar pagamento valor devo ver verificar mês mes passado anterior "
    "número numero também tambem então entao porque porquê isso isto esse essa lá aqui vou vai são sou estou "
    "obrigadíssimo certo exato exatamente claro pois".split()
)
_EN_WORDS = frozenset(
    "the my i is are was what where hi hello please thanks thank can you about and to of it that yes when how "
    "do does bill status goodbye bye okay ok your me this with for on at have has had would like want need "
    "tell check know much owe due latest previous actually forget meant right correct great".split()
)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def detect(text: str, previous: str = DEFAULT) -> str:
    """Return ``pt`` or ``en`` for the utterance; keep ``previous`` when it is a tie or has no clues."""
    words = [w.lower() for w in _WORD.findall(text or "")]
    pt = sum(1 for w in words if w in _PT_WORDS)
    en = sum(1 for w in words if w in _EN_WORDS)
    if pt > en:
        return PT
    if en > pt:
        return EN
    return previous


_STRINGS: dict[str, dict[str, str]] = {
    "empty_input": {
        PT: "Desculpe, não percebi. Pode repetir, por favor?",
        EN: "Sorry, I didn't catch that. Could you say it again?",
    },
    "ack": {PT: "Já vou verificar isso.", EN: "On it, checking that now."},
    "closing": {PT: "Posso ajudar em mais alguma coisa?", EN: "Is there anything else I can help with?"},
    "goodbye": {PT: "Obrigado pela chamada. Tenha um ótimo dia!", EN: "Thanks for calling. Have a great day!"},
    "progress0": {PT: "Ainda estou a verificar, só um momento.", EN: "Still working on it, just a moment."},
    "progress1": {PT: "Quase lá, obrigado pela paciência.", EN: "Almost there, thanks for your patience."},
    "progress2": {
        PT: "Ainda estou a verificar, está a demorar um pouco mais do que o habitual.",
        EN: "Still checking, this is taking a little longer than usual.",
    },
    "still_working": {
        PT: "Ainda estou a tratar disso. Digo-lhe assim que estiver pronto.",
        EN: "I'm still working on that. I'll share it as soon as it's ready.",
    },
    "cancelled": {PT: "Sem problema, parei isso. ", EN: "No problem, I've stopped that. "},
    "reask": {PT: "Desculpe, não percebi. Posso avançar?", EN: "Sorry, I didn't catch that. Shall I go ahead?"},
    "open_question": {
        PT: "Em que posso ajudar hoje, com a sua fatura ou com uma encomenda?",
        EN: "What can I help you with today, your bill or an order?",
    },
    "say_again": {
        PT: "Desculpe, pode repetir? Posso ajudar com a sua fatura ou com uma encomenda.",
        EN: "Sorry, could you say that again? I can help with your bill or an order.",
    },
    "finishing": {PT: "Com certeza. Só estou a acabar de verificar {label}.", EN: "Sure. I'm just finishing {label}."},
    "still_checking": {PT: "Ainda estou a verificar {label}.", EN: "I'm still checking {label}."},
    "already_checking": {PT: "Já estou a verificar {label}.", EN: "I'm already checking {label}."},
    "change": {PT: "Sem problema. O que gostaria que alterasse?", EN: "No problem. What would you like me to change?"},
    "which_one": {
        PT: "Posso ajudar com a sua fatura ou com o estado de uma encomenda. Qual prefere?",
        EN: "I can help with your bill or the status of an order. Which would you like?",
    },
    "ask_order": {PT: "Com certeza. Qual é o número da encomenda?", EN: "Sure. What's the order number?"},
    "confirm": {PT: "Então quer que eu {plan}. Está correto?", EN: "So you'd like me to {plan}. Is that right?"},
    "update": {PT: "Aqui está a atualização sobre {label}. {result}", EN: "Here's the update on {label}. {result}"},
    "failed": {
        PT: "Desculpe, não consegui verificar {label} neste momento. Quer que tente novamente?",
        EN: "Sorry, I couldn't finish checking {label} just now. Would you like me to try again?",
    },
    "greeting": {
        PT: "Olá, obrigado por ligar. Em que posso ajudar? Posso esclarecer questões da sua fatura ou ver o estado de uma encomenda.",
        EN: "Hello, thanks for calling. How can I help you today? I can look at your bill or the status of an order.",
    },
    "your_bill": {PT: "a sua fatura", EN: "your bill"},
    "your_prev_bill": {PT: "a sua fatura anterior", EN: "your previous bill"},
    "your_latest_bill": {PT: "a sua última fatura", EN: "your latest bill"},
    "order_n": {PT: "a encomenda {order_id}", EN: "order {order_id}"},
}


def t(key: str, lang: str = DEFAULT, **fmt: str) -> str:
    entry = _STRINGS[key]
    text = entry.get(lang) or entry[DEFAULT]
    return text.format(**fmt) if fmt else text
