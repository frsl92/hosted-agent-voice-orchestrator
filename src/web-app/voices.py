"""pt-PT voices the tester can pick from, and the locale every Voice Live session is forced to."""

from __future__ import annotations

from typing import Any

# Voice Live is always told to speak pt-PT, whatever voice is selected. Voices designed for other
# languages can be added later and will still be forced to this locale.
LOCALE = "pt-PT"

_EXPRESSIVE = [
    "angry", "confused", "determined", "embarrassed", "excited", "happy", "hopeful",
    "joyful", "regretful", "relieved", "sad", "softvoice", "surprised",
]


def _voice(name: str, label: str, gender: str, tier: str, status: str, styles: list[str] | None = None) -> dict[str, Any]:
    return {"id": name, "name": label, "gender": gender, "tier": tier, "status": status, "styles": styles or []}


VOICES: list[dict[str, Any]] = [
    _voice("pt-PT-Rui:MAI-Voice-2-Flash", "Rui · MAI-Voice-2-Flash", "Male", "Neural", "Preview", _EXPRESSIVE),
    _voice("pt-PT-Rui:MAI-Voice-2.1-Flash", "Rui · MAI-Voice-2.1-Flash", "Male", "Neural", "Preview", _EXPRESSIVE),
    _voice("pt-PT-Grant:MAI-Voice-2.1-Flash", "Grant · MAI-Voice-2.1-Flash", "Male", "Neural", "Preview"),
    _voice("pt-PT-Harper:MAI-Voice-2.1-Flash", "Harper · MAI-Voice-2.1-Flash", "Female", "Neural", "Preview"),
    _voice("pt-PT-Rui:MAI-Voice-2", "Rui · MAI-Voice-2", "Male", "NeuralHD", "Preview", _EXPRESSIVE),
    _voice("pt-PT-Rui:MAI-Voice-2.1", "Rui · MAI-Voice-2.1", "Male", "NeuralHD", "Preview", _EXPRESSIVE),
    _voice("pt-PT-Grant:MAI-Voice-2.1", "Grant · MAI-Voice-2.1", "Male", "NeuralHD", "Preview"),
    _voice("pt-PT-Harper:MAI-Voice-2.1", "Harper · MAI-Voice-2.1", "Female", "NeuralHD", "Preview"),
    _voice("pt-PT-DuarteNeural", "Duarte", "Male", "Neural", "GA"),
    _voice("pt-PT-FernandaNeural", "Fernanda", "Female", "Neural", "GA"),
    _voice("pt-PT-RaquelNeural", "Raquel", "Female", "Neural", "GA"),
]

DEFAULT_VOICE = VOICES[0]["id"]
_BY_ID = {v["id"]: v for v in VOICES}


def resolve(voice: str | None, style: str | None) -> tuple[str, str | None]:
    """Validated (voice, style). Unknown voices fall back to the default; styles the voice lacks are dropped."""
    entry = _BY_ID.get(voice or "") or _BY_ID[DEFAULT_VOICE]
    return entry["id"], style if style in entry["styles"] else None
