"""Interfaz que debe implementar cualquier portal inmobiliario."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.models import Listing


class Source(ABC):
    """Un origen de anuncios (Idealista, Fotocasa, un mock...)."""

    #: Identificador corto, se usa en logs y en la BD.
    name: str = "source"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    @abstractmethod
    def fetch(self) -> list[Listing]:
        """Devuelve los anuncios disponibles ahora mismo, sin filtrar ni deduplicar."""
        raise NotImplementedError
