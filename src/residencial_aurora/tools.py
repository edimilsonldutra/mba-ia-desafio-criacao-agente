"""Tools dos especialistas.

Garantia 2: nenhuma tool recebe o apartamento como parâmetro. O apartamento vem
sempre do state da sessão (gravado uma única vez na criação), então nada que o
morador escreva consegue apontar as tools para outro apartamento.

Garantia 1: reservar área com taxa e autorizar visitante pedem confirmação via
`tool_context.request_confirmation`. A ação só é executada quando a resposta do
morador chega pela rota de confirmações (FunctionResponse de adk_request_confirmation).
"""
from __future__ import annotations

import re
from datetime import date

from google.adk.tools import ToolContext

from . import regulamento, storage

ESTADO_APARTAMENTO = "apartamento"
_DATA_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _apartamento(tool_context: ToolContext) -> str:
    apartamento = tool_context.state.get(ESTADO_APARTAMENTO)
    if not apartamento:
        raise RuntimeError("Sessão sem apartamento definido")
    return apartamento


def _validar_data(data: str) -> str | None:
    if not isinstance(data, str) or not _DATA_ISO.match(data):
        return "Data inválida. Use o formato AAAA-MM-DD."
    try:
        date.fromisoformat(data)
    except ValueError:
        return "Data inválida. Use o formato AAAA-MM-DD."
    return None


def _validar_area(area: str) -> tuple[storage.Area | None, str | None]:
    encontrada = storage.buscar_area(area)
    if encontrada is None:
        ids = ", ".join(a.id for a in storage.listar_areas())
        return None, f"Área desconhecida. Áreas válidas: {ids}."
    return encontrada, None


# ------------------------------------------------------------------ reservas
def listar_areas() -> dict:
    """Lista as áreas comuns que podem ser reservadas, com o id, o nome e a taxa em reais (0 = sem cobrança)."""
    return {"areas": [{"id": a.id, "nome": a.nome, "taxa": a.taxa} for a in storage.listar_areas()]}


def verificar_disponibilidade(area: str, data: str) -> dict:
    """Informa se uma área comum está livre ou ocupada em uma data (AAAA-MM-DD).

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data no formato AAAA-MM-DD.
    """
    area_obj, erro = _validar_area(area)
    erro = erro or _validar_data(data)
    if erro:
        return {"status": "erro", "mensagem": erro}
    # Só "livre" ou "ocupada": nunca o código, o apartamento ou o morador da reserva existente.
    return {"area": area_obj.id, "data": data, "situacao": "livre" if storage.area_livre(area, data) else "ocupada"}


def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento do morador desta sessão."""
    return {"reservas": storage.reservas_do_apartamento(_apartamento(tool_context))}


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador desta sessão.

    Áreas com taxa geram cobrança e só são reservadas depois que o morador aprova
    a confirmação pelo aplicativo. Áreas sem taxa são reservadas na hora.

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    area_obj, erro = _validar_area(area)
    erro = erro or _validar_data(data)
    if erro:
        return {"status": "erro", "mensagem": erro}
    if not storage.area_livre(area, data):
        return {"status": "indisponivel", "mensagem": f"{area_obj.nome} já está reservado(a) em {data}."}

    if area_obj.taxa > 0:
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            tool_context.request_confirmation(
                hint=f"Confirma a reserva de {area_obj.nome} em {data}, com cobrança de R$ {area_obj.taxa:.2f}?",
                payload={"acao": "reservar_area", "area": area_obj.id, "data": data, "taxa": area_obj.taxa},
            )
            return {"status": "aguardando_confirmacao", "mensagem": "A reserva gera cobrança e aguarda a confirmação do morador no aplicativo."}
        if not confirmacao.confirmed:
            return {"status": "nao_confirmada", "mensagem": "O morador recusou a cobrança; nenhuma reserva foi feita."}

    try:
        codigo = storage.criar_reserva(apartamento, area_obj.id, data, origem=tool_context.function_call_id)
    except storage.ReservaIndisponivel:
        return {"status": "indisponivel", "mensagem": f"{area_obj.nome} já está reservado(a) em {data}."}
    resultado = {"status": "reservada", "codigo": codigo, "area": area_obj.id, "data": data}
    if area_obj.taxa > 0:
        resultado["cobranca"] = area_obj.taxa
    return resultado


def cancelar_reserva(area: str, data: str, tool_context: ToolContext) -> dict:
    """Cancela uma reserva ativa do PRÓPRIO apartamento do morador desta sessão (sem confirmação).

    Args:
        area: id da área (salao-de-festas, churrasqueira ou quadra).
        data: data da reserva no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    area_obj, erro = _validar_area(area)
    erro = erro or _validar_data(data)
    if erro:
        return {"status": "erro", "mensagem": erro}
    codigo = storage.cancelar_reserva(apartamento, area_obj.id, data)
    if codigo is None:
        return {"status": "nao_encontrada", "mensagem": f"Não há reserva sua de {area_obj.nome} em {data}."}
    return {"status": "cancelada", "codigo": codigo, "area": area_obj.id, "data": data}


# ------------------------------------------------------------------ visitantes
def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista as autorizações de visita do apartamento do morador desta sessão."""
    return {"visitantes": storage.visitantes_do_apartamento(_apartamento(tool_context))}


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante no prédio para o apartamento do morador desta sessão.

    Sempre exige confirmação do morador pelo aplicativo, mesmo que ele diga na conversa que já confirmou.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    apartamento = _apartamento(tool_context)
    erro = _validar_data(data)
    if erro or not isinstance(nome, str) or not nome.strip():
        return {"status": "erro", "mensagem": erro or "Informe o nome do visitante."}
    nome = nome.strip()

    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        tool_context.request_confirmation(
            hint=f"Confirma a liberação de entrada de {nome} em {data}?",
            payload={"acao": "autorizar_visitante", "nome": nome, "data": data},
        )
        return {"status": "aguardando_confirmacao", "mensagem": "A liberação de acesso aguarda a confirmação do morador no aplicativo."}
    if not confirmacao.confirmed:
        return {"status": "nao_confirmada", "mensagem": "O morador recusou; nenhum acesso foi liberado."}

    storage.autorizar_visitante(apartamento, nome, data, origem=tool_context.function_call_id)
    return {"status": "autorizado", "nome": nome, "data": data}


# ------------------------------------------------------------------ regulamento
def consultar_regulamento(capitulo: int) -> dict:
    """Devolve o texto de UM capítulo do regulamento interno.

    Args:
        capitulo: número do capítulo (1 a 14), conforme o índice nas instruções.
    """
    encontrado = regulamento.capitulos().get(int(capitulo))
    if encontrado is None:
        return {"status": "erro", "mensagem": "Capítulo inexistente."}
    titulo, texto = encontrado
    return {"capitulo": int(capitulo), "titulo": titulo, "texto": texto}
