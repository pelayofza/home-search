"""Normalización de texto, compartida por el filtrado y el scoring."""

from __future__ import annotations

import unicodedata


def normalize(text: str) -> str:
    """Minúsculas y sin tildes, para comparar barrios y palabras sin sorpresas."""
    nfkd = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))
