"""API HTTP do assistente (contrato do enunciado)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from google.genai import errors as genai_errors
from pydantic import BaseModel

from . import storage
from .service import Assistente, ConfirmacaoInvalida, SessaoNaoEncontrada

logger = logging.getLogger(__name__)
_estado: dict[str, Assistente] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _estado["assistente"] = Assistente()
    yield
    _estado.clear()


app = FastAPI(title="Assistente do Residencial Aurora", lifespan=lifespan)


def assistente() -> Assistente:
    return _estado["assistente"]


class NovaSessao(BaseModel):
    apartamento: str


class NovaMensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


@app.exception_handler(SessaoNaoEncontrada)
async def _sessao_inexistente(_request, _exc):
    return JSONResponse(status_code=404, content={"detail": "Sessão não encontrada"})


@app.exception_handler(ConfirmacaoInvalida)
async def _confirmacao_invalida(_request, _exc):
    return JSONResponse(status_code=409, content={"detail": "Não existe confirmação pendente com esse id nesta sessão"})


@app.exception_handler(genai_errors.APIError)
async def _modelo_indisponivel(_request, exc: genai_errors.APIError):
    if exc.code in (429, 500, 503, 504):  # retry do agents.RETRY_GEMINI esgotado
        logger.warning("Gemini indisponível depois das tentativas: %s", exc.code)
        return JSONResponse(status_code=503, headers={"Retry-After": "60"},
                            content={"detail": "Assistente temporariamente indisponível; tente de novo em instantes"})
    raise exc


@app.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao):
    if not storage.apartamento_existe(corpo.apartamento):
        raise HTTPException(status_code=422, detail="Apartamento inexistente")
    return {"session_id": await assistente().criar_sessao(corpo.apartamento)}


@app.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: NovaMensagem):
    resultado = await assistente().enviar_mensagem(session_id, corpo.texto)
    return resultado.como_json()


@app.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, corpo: RespostaConfirmacao):
    resultado = await assistente().responder_confirmacao(session_id, corpo.id, corpo.confirmado)
    return resultado.como_json()


@app.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str):
    return await assistente().eventos(session_id)


# ------------------------------------------------------------ rotas de verificação (sem modelo)
@app.get("/apartamentos/{numero}/reservas")
async def reservas_do_apartamento(numero: str):
    return storage.reservas_do_apartamento(numero)


@app.get("/apartamentos/{numero}/visitantes")
async def visitantes_do_apartamento(numero: str):
    return storage.visitantes_do_apartamento(numero)
