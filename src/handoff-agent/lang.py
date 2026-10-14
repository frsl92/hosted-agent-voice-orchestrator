"""Tiny language helper: the call defaults to European Portuguese, English is the second language.

Only used for the few fixed (non-LLM) sentences; everything the LLM says follows the customer's language
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
    "fallback": {
        PT: "Desculpe, tive um problema do meu lado. Pode repetir, por favor?",
        EN: "Sorry, I had a problem on my side. Could you please say that again?",
    },
}


def t(key: str, lang: str = DEFAULT) -> str:
    entry = _STRINGS[key]
    return entry.get(lang) or entry[DEFAULT]
