"""Garantia 4: o regulamento é consultado por capítulo, nunca carregado inteiro.

A tool devolve apenas o texto do capítulo pedido; só ele entra no histórico da sessão.
"""
from __future__ import annotations

import re
from functools import lru_cache

from . import config

_CAPITULO = re.compile(r"^## Capítulo ([IVXLC]+): (.+)$", re.MULTILINE)
_ROMANOS = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def _romano_para_int(romano: str) -> int:
    total, anterior = 0, 0
    for simbolo in reversed(romano):
        valor = _ROMANOS[simbolo]
        total += -valor if valor < anterior else valor
        anterior = max(anterior, valor)
    return total


@lru_cache(maxsize=1)
def capitulos() -> dict[int, tuple[str, str]]:
    """{numero: (titulo, texto)} a partir de dados/regulamento.md."""
    texto = (config.DADOS_DIR / "regulamento.md").read_text(encoding="utf-8")
    marcas = list(_CAPITULO.finditer(texto))
    resultado = {}
    for i, marca in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        resultado[_romano_para_int(marca.group(1))] = (marca.group(2).strip(), texto[marca.start():fim].strip())
    return resultado


def indice() -> str:
    """Só os títulos (para orientar o especialista a escolher o capítulo)."""
    return "\n".join(f"{numero}. {titulo}" for numero, (titulo, _) in sorted(capitulos().items()))
