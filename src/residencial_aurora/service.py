"""Execução dos agentes: Runner + App resumível + sessões persistidas em SQLite.

Garantia 3: sessões e eventos vivem no DatabaseSessionService (var/sessoes.db) e os dados
do condomínio em var/condominio.db; reiniciar a API não perde nada.

Garantia 1: a lista de confirmações pendentes é derivada dos eventos da sessão:
toda chamada `adk_request_confirmation` sem FunctionResponse correspondente está pendente.
Responder exige um id pendente nesta sessão; a resposta vira uma FunctionResponse e,
como o App é resumível, o Runner entrega ao agente que pediu a confirmação.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from google.adk.apps import App, ResumabilityConfig
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from . import config, storage
from .agents import root_agent
from .tools import ESTADO_APARTAMENTO

CONFIRMACAO = "adk_request_confirmation"


class SessaoNaoEncontrada(Exception):
    pass


class ConfirmacaoInvalida(Exception):
    pass


@dataclass
class Resultado:
    resposta: str
    confirmacoes_pendentes: list[dict]

    def como_json(self) -> dict:
        resposta = self.resposta
        if not resposta.strip() and self.confirmacoes_pendentes:  # o modelo parou esperando a confirmação
            resposta = " ".join(p["descricao"] for p in self.confirmacoes_pendentes) + " Confirme pelo aplicativo."
        return {"resposta": resposta, "confirmacoes_pendentes": self.confirmacoes_pendentes}


class Assistente:
    def __init__(self, agent=root_agent, sessoes_db_url: str | None = None):
        config.VAR_DIR.mkdir(parents=True, exist_ok=True)
        storage.init_db()
        self.session_service = DatabaseSessionService(
            db_url=sessoes_db_url or f"sqlite+aiosqlite:///{config.SESSOES_DB.as_posix()}",
            connect_args={"timeout": 30},
        )
        self.app = App(
            name=config.APP_NAME,
            root_agent=agent,
            # Resumível: uma FunctionResponse é roteada ao agente que fez a chamada correspondente
            # (inclusive depois de reiniciar a API), e não de volta ao agente principal.
            resumability_config=ResumabilityConfig(is_resumable=True),
        )
        self.runner = Runner(app=self.app, session_service=self.session_service)
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------ sessões
    @staticmethod
    def _user_id(apartamento: str) -> str:
        return f"apartamento-{apartamento}"

    async def criar_sessao(self, apartamento: str) -> str:
        session_id = uuid.uuid4().hex
        # O apartamento é gravado uma única vez, no state da sessão e na tabela de sessões.
        await self.session_service.create_session(
            app_name=config.APP_NAME,
            user_id=self._user_id(apartamento),
            session_id=session_id,
            state={ESTADO_APARTAMENTO: apartamento},
        )
        storage.registrar_sessao(session_id, apartamento)
        return session_id

    async def _sessao(self, session_id: str):
        apartamento = storage.apartamento_da_sessao(session_id)
        if apartamento is None:
            raise SessaoNaoEncontrada(session_id)
        sessao = await self.session_service.get_session(
            app_name=config.APP_NAME, user_id=self._user_id(apartamento), session_id=session_id
        )
        if sessao is None:
            raise SessaoNaoEncontrada(session_id)
        return apartamento, sessao

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def eventos(self, session_id: str) -> list[dict]:
        _, sessao = await self._sessao(session_id)
        return [evento.model_dump(mode="json", exclude_none=True, by_alias=True) for evento in sessao.events]

    # ------------------------------------------------------------ confirmações
    @staticmethod
    def _pendentes(eventos) -> list[dict]:
        respondidas = {
            resposta.id
            for evento in eventos
            for resposta in evento.get_function_responses()
            if resposta.name == CONFIRMACAO
        }
        pendentes = []
        for evento in eventos:
            for chamada in evento.get_function_calls():
                if chamada.name != CONFIRMACAO or chamada.id in respondidas:
                    continue
                confirmacao = (chamada.args or {}).get("toolConfirmation", {})
                payload = dict(confirmacao.get("payload") or {})
                acao = payload.pop("acao", (chamada.args or {}).get("originalFunctionCall", {}).get("name"))
                pendentes.append({
                    "id": chamada.id,
                    "acao": acao,
                    "detalhes": payload,
                    "descricao": confirmacao.get("hint", ""),
                })
        return pendentes

    # ------------------------------------------------------------ execução
    async def _executar(self, apartamento: str, session_id: str, mensagem: types.Content) -> Resultado:
        textos = []
        async for evento in self.runner.run_async(
            user_id=self._user_id(apartamento), session_id=session_id, new_message=mensagem
        ):
            if evento.author == "user" or evento.partial or not evento.content:
                continue
            for parte in evento.content.parts or []:
                if parte.text and not parte.thought:
                    textos.append(parte.text.strip())
        _, sessao = await self._sessao(session_id)
        return Resultado(resposta="\n".join(t for t in textos if t), confirmacoes_pendentes=self._pendentes(sessao.events))

    async def enviar_mensagem(self, session_id: str, texto: str) -> Resultado:
        apartamento, _ = await self._sessao(session_id)
        async with self._lock(session_id):
            conteudo = types.Content(role="user", parts=[types.Part(text=texto)])
            return await self._executar(apartamento, session_id, conteudo)

    async def responder_confirmacao(self, session_id: str, confirmacao_id: str, confirmado: bool) -> Resultado:
        apartamento, _ = await self._sessao(session_id)
        async with self._lock(session_id):
            # Revalida dentro do lock: um id já respondido (ou de outra sessão) não está mais pendente.
            _, sessao = await self._sessao(session_id)
            if confirmacao_id not in {p["id"] for p in self._pendentes(sessao.events)}:
                raise ConfirmacaoInvalida(confirmacao_id)
            resposta = types.Content(
                role="user",
                parts=[types.Part(function_response=types.FunctionResponse(
                    id=confirmacao_id, name=CONFIRMACAO, response={"confirmed": bool(confirmado)},
                ))],
            )
            return await self._executar(apartamento, session_id, resposta)
