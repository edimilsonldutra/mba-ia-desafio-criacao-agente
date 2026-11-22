# Assistente virtual do Residencial Aurora

API em Python (FastAPI) com um assistente construído em **Google ADK 2.9.1** sobre modelos Gemini. Pelo chat, o morador reserva áreas comuns, cancela as próprias reservas, autoriza visitantes e tira dúvidas sobre o regulamento. Nenhuma mensagem consegue furar as regras do condomínio, porque **o modelo decide o caminho e o código decide o que é permitido**.

Enunciado original: [repositório base](https://github.com/devfullcycle/mba-ia-desafio-criacao-agente).

## Arquitetura

```
POST /sessoes/{id}/mensagens ─► Runner (App resumível) ─► concierge (agente principal, sem tools)
POST /sessoes/{id}/confirmacoes        │                     │ transfer_to_agent
                                       │       ┌─────────────┼──────────────┐
                                       │       ▼             ▼              ▼
                                       │   reservas      visitantes     regulamento
                                       │   (5 tools)     (2 tools)      (1 tool: um capítulo por vez)
                                       │       │             │
DatabaseSessionService (var/sessoes.db)        └──── storage.py ─► var/condominio.db (SQLite)
```

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| **concierge** (`agents.py`, `name="concierge"`) | Entende o pedido e encaminha. Não tem tools e **não recebe o regulamento** | Agente raiz do `App`; recebe toda mensagem nova que não seja continuação de um especialista | Mantém o prompt principal curto e barato. Sem tools, não existe caminho para ele executar ação sem as regras dos especialistas |
| **reservas** | `listar_areas`, `verificar_disponibilidade`, `listar_minhas_reservas`, `reservar_area`, `cancelar_reserva` | *Sub-agent* via `transfer_to_agent` | Concentra a regra de cobrança e as escritas em reservas num único dono, que também é quem recebe a confirmação de volta |
| **visitantes** | `listar_meus_visitantes`, `autorizar_visitante` | *Sub-agent* via `transfer_to_agent` | Liberação de acesso é sensível e sempre pede confirmação; isolar o domínio deixa isso explícito |
| **regulamento** | `consultar_regulamento(capitulo)` | *Sub-agent* via `transfer_to_agent` | Só ele sabe que o regulamento existe (conhece apenas o índice de títulos) e traz para a conversa **um** capítulo por vez |

Escolhi transferência (`sub_agents`) em vez de `AgentTool` porque o especialista que pede uma confirmação precisa ser o mesmo que a recebe de volta. Com o `App` resumível, o `Runner` entrega a resposta ao agente autor da chamada (`google/adk/agents/_agent_router.py`, `find_agent_to_run`). Depois que um especialista responde, ele continua atendendo enquanto o assunto for dele e transfere de volta ao `concierge` quando não for.

Modelo: configurável por `AURORA_MODEL` (padrão `gemini-flash-lite-latest`, um alias do Google AI Studio para o Flash-Lite vigente). Escolhi esse padrão porque o plano gratuito limita requisições **por dia e por modelo**. No `gemini-2.5-flash` o limite era de 20 por dia quando testei, menos do que o fluxo do avaliador consome (cerca de 40 chamadas). O fluxo completo (passos 1 a 14) foi validado com o `gemini-flash-lite-latest`; com uma chave paga, qualquer Flash serve.

Cada agente usa o modelo com `retry_options` (`agents.py`, `RETRY_GEMINI`): em 429/500/503/504 ele espera com backoff exponencial (5s até 60s, 8 tentativas) e tenta de novo, então um pico de demanda deixa a mensagem mais lenta, mas não falha. Se as tentativas se esgotarem, a API responde **503** com `Retry-After` (`api.py`), nunca 500.

## Garantias

### 1. Cobrança ou acesso só com confirmação
- **Onde:** `src/residencial_aurora/tools.py`. `reservar_area` (linha 75) chama `tool_context.request_confirmation(...)` (linha 96) quando `area_obj.taxa > 0`, e `autorizar_visitante` (linha 138) sempre chama (linha 155). Sem `tool_context.tool_confirmation`, a tool **retorna sem gravar**. Com `confirmed=False`, retorna `nao_confirmada`. Só com `confirmed=True` chega a `storage.criar_reserva` / `storage.autorizar_visitante`.
- **Rota de confirmações:** `src/residencial_aurora/service.py`. `_pendentes` (linha 101) deriva as pendências dos eventos da sessão: toda chamada `adk_request_confirmation` sem `FunctionResponse` correspondente está pendente. Em `responder_confirmacao`, um id que não esteja pendente **nesta** sessão (inexistente, de outra sessão ou já respondido) levanta `ConfirmacaoInvalida` (linha 150), que a API converte em **409** antes de qualquer execução.
- **Por que não depende do modelo:** a confirmação é uma `FunctionResponse` que só a rota `/confirmacoes` cria. Texto do morador ("já estou confirmando") nunca vira `tool_confirmation`. O `App` usa `ResumabilityConfig(is_resumable=True)` (linha 59), e é isso que faz a resposta chegar ao especialista que pediu, inclusive depois de reiniciar a API. Executar uma única vez é garantido em dois níveis: o id deixa de estar pendente e a gravação é idempotente pela coluna `origem TEXT UNIQUE` (`storage.py` linhas 25 e 36, com o `function_call_id` da tool).

### 2. Cada sessão pertence a um apartamento
- **Onde:** o apartamento é gravado uma vez em `service.py` `criar_sessao`, no `state` da sessão ADK (linha 76) e na tabela `sessoes` (linha 78). Todas as tools obtêm o apartamento por `tools.py` `_apartamento` (linhas 24-25), via `tool_context.state.get("apartamento")`.
- **Nenhuma tool aceita apartamento como parâmetro.** `cancelar_reserva` (linha 114) filtra por `apartamento = ?` no SQL (`storage.cancelar_reserva`). `verificar_disponibilidade` (linha 55) devolve só `"situacao": "livre" | "ocupada"` (linha 67), nunca o código nem o dono.
- **Por que não depende do modelo:** mesmo que o morador diga ser do 302 e o modelo "acredite", as tools não têm como receber outro apartamento, e nenhuma consulta devolve dados de terceiros.

### 3. Nada se perde no reinício
- **Onde:** `service.py` linha 50: `DatabaseSessionService("sqlite+aiosqlite:///var/sessoes.db")` guarda sessões e eventos. Reservas e visitantes ficam em `var/condominio.db` (`storage.py`). O `App` resumível (linha 59) permite continuar a sessão e responder confirmações pedidas antes do restart.
- **Códigos que nunca repetem:** a tabela `codigos_emitidos` (`storage.py` linha 42) registra todo código já gerado e nunca é apagada, nem pela restauração. `_novo_codigo` sorteia até achar um código inédito. Reservas canceladas continuam na tabela, com `status = 'cancelada'`.

### 4. O regulamento é consultado, não carregado
- **Onde:** `src/residencial_aurora/regulamento.py`. `capitulos()` (linha 26) indexa `dados/regulamento.md` por capítulo e `indice()` (linha 37) expõe só os títulos. A tool `consultar_regulamento` (`tools.py` linha 168) devolve **o texto de um único capítulo**.
- **Agente principal:** `agents.py` (`name="concierge"`, linha 81) não recebe o regulamento nem o índice. Só o especialista `regulamento` (linha 64) conhece os títulos.
- **Por que não depende do modelo:** a única forma de o texto entrar no histórico é a resposta da tool, que por construção contém um capítulo só. A pergunta sobre a piscina traz apenas o Capítulo IV ("Aos domingos e feriados, a piscina funciona das 9h às 20h").

### 5. Dois moradores, uma reserva
- **Onde:** `storage.py` linha 29:
  ```sql
  CREATE UNIQUE INDEX ux_reserva_ativa_area_data ON reservas (area, data) WHERE status = 'ativa';
  ```
  `criar_reserva` (linha 161) grava dentro de `BEGIN IMMEDIATE`. Se outra reserva ativa entrou antes, o `INSERT` falha com `IntegrityError` e vira `ReservaIndisponivel` (linha 185). A tool responde "indisponível" normalmente, com HTTP 200 e sem erro de servidor.
- **Por que não depende do modelo (nem da conferência prévia):** a checagem de disponibilidade antes de pedir a confirmação é só uma gentileza. A exclusividade é imposta pelo banco **no instante da gravação**, então duas aprovações simultâneas nunca produzem duas reservas ativas.

### Testes das garantias (sem chave)
`tests/test_garantias.py` substitui o Gemini por um LLM roteirizado e exercita a pilha real: Runner, App resumível, `DatabaseSessionService`, tools e SQLite. Os cenários são:
- negar e aprovar;
- 409 no reenvio e para id inexistente;
- aprovação **depois de recriar o serviço** (restart);
- isolamento do 302;
- eventos preservados no reinício;
- regulamento por capítulo;
- **duas aprovações concorrentes gerando uma única reserva**.

```bash
uv run pytest -q
```

## Como rodar

**Pré-requisitos:** Python 3.12+, [uv](https://docs.astral.sh/uv/) e uma chave do [Google AI Studio](https://aistudio.google.com/apikey). Não há serviço externo: o armazenamento é SQLite em `var/`.

**Variáveis (`.env`):**

| Variável | Obrigatória | Descrição |
|---|---|---|
| `GOOGLE_API_KEY` | sim | Chave do Google AI Studio |
| `GOOGLE_GENAI_USE_VERTEXAI` | não | `FALSE` (usa o AI Studio, não a Vertex AI) |
| `AURORA_MODEL` | não | Modelo Gemini dos agentes (padrão `gemini-flash-lite-latest`) |

```bash
cp .env.example .env          # preencha GOOGLE_API_KEY
uv sync
uv run aurora-restore         # restaura reservas e visitantes de dados/*.json e apaga as sessões
uv run aurora-api             # sobe a API em http://localhost:8000
```

Para reiniciar sem perder nada, pare com `Ctrl+C` e rode `uv run aurora-api` de novo, sem restaurar.

**Validar o fluxo completo do avaliador** (passos 1 a 14), com a API no ar:

```bash
uv run python scripts/fluxo_avaliador.py antes     # passos 1-12
# Ctrl+C na API, suba de novo com `uv run aurora-api`
uv run python scripts/fluxo_avaliador.py depois    # passos 13-14
```
