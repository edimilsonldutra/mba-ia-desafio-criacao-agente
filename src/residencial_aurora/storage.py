"""Armazenamento do condomínio (SQLite).

Garantia 5: a exclusividade de uma área por data é imposta pelo próprio banco,
com um índice único parcial sobre as reservas ativas. A gravação é a checagem:
se outra reserva ativa entrou antes, o INSERT falha e a reserva é recusada.
"""
from __future__ import annotations

import json
import random
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservas (
    codigo        TEXT PRIMARY KEY,
    apartamento   TEXT NOT NULL,
    area          TEXT NOT NULL,
    data          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'ativa' CHECK (status IN ('ativa', 'cancelada')),
    origem        TEXT UNIQUE,              -- function_call_id da tool: torna a gravação idempotente
    criada_em     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- Garantia 5: no máximo uma reserva ATIVA por área e data, verificado no instante da gravação.
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa_area_data ON reservas (area, data) WHERE status = 'ativa';

CREATE TABLE IF NOT EXISTS visitantes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento   TEXT NOT NULL,
    nome          TEXT NOT NULL,
    data          TEXT NOT NULL,
    origem        TEXT UNIQUE,
    criado_em     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Todo código já emitido (inclusive de reservas canceladas ou de antes de uma restauração).
-- Nunca é apagado, para que um código novo jamais repita um anterior.
CREATE TABLE IF NOT EXISTS codigos_emitidos (codigo TEXT PRIMARY KEY);

-- Dono de cada sessão, definido uma única vez na criação.
CREATE TABLE IF NOT EXISTS sessoes (
    session_id    TEXT PRIMARY KEY,
    apartamento   TEXT NOT NULL,
    criada_em     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class ReservaIndisponivel(Exception):
    """Outra reserva ativa já ocupa a área na data."""


@dataclass(frozen=True)
class Area:
    id: str
    nome: str
    taxa: float


def _connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.CONDOMINIO_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; transações explícitas
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    with closing(_connect()) as conn:
        conn.executescript(SCHEMA)


def _carregar_json(nome: str):
    return json.loads((config.DADOS_DIR / nome).read_text(encoding="utf-8"))


def restaurar_dados_iniciais() -> None:
    """Volta reservas e visitantes ao estado de dados/*.json (mantém o histórico de códigos)."""
    init_db()
    reservas = _carregar_json("reservas.json")
    visitantes = _carregar_json("visitantes.json")
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM reservas")
        conn.execute("DELETE FROM visitantes")
        conn.execute("DELETE FROM sessoes")
        conn.executemany(
            "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
            [(r["codigo"], r["apartamento"], r["area"], r["data"]) for r in reservas],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO codigos_emitidos (codigo) VALUES (?)",
            [(r["codigo"],) for r in reservas],
        )
        conn.executemany(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            [(v["apartamento"], v["nome"], v["data"]) for v in visitantes],
        )
        conn.execute("COMMIT")


# ------------------------------------------------------------------ catálogos
def listar_areas() -> list[Area]:
    return [Area(a["id"], a["nome"], float(a["taxa"])) for a in _carregar_json("areas.json")]


def buscar_area(area_id: str) -> Area | None:
    return next((a for a in listar_areas() if a.id == area_id), None)


def apartamento_existe(numero: str) -> bool:
    return any(a["numero"] == numero for a in _carregar_json("apartamentos.json"))


# ------------------------------------------------------------------ sessões
def registrar_sessao(session_id: str, apartamento: str) -> None:
    with closing(_connect()) as conn:
        conn.execute("INSERT INTO sessoes (session_id, apartamento) VALUES (?, ?)", (session_id, apartamento))


def apartamento_da_sessao(session_id: str) -> str | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT apartamento FROM sessoes WHERE session_id = ?", (session_id,)).fetchone()
    return row["apartamento"] if row else None


# ------------------------------------------------------------------ reservas
def area_livre(area_id: str, data: str) -> bool:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'", (area_id, data)
        ).fetchone()
    return row is None


def reservas_do_apartamento(apartamento: str) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT codigo, area, data FROM reservas WHERE apartamento = ? AND status = 'ativa' ORDER BY data, codigo",
            (apartamento,),
        ).fetchall()
    return [dict(r) for r in rows]


def _novo_codigo(conn: sqlite3.Connection) -> str:
    while True:
        codigo = f"RSV-{random.randint(1000, 9999)}"
        try:
            conn.execute("INSERT INTO codigos_emitidos (codigo) VALUES (?)", (codigo,))
            return codigo
        except sqlite3.IntegrityError:
            continue  # código já usado em algum momento: sorteia outro


def criar_reserva(apartamento: str, area_id: str, data: str, origem: str) -> str:
    """Grava a reserva de forma atômica. Levanta ReservaIndisponivel se a data já estiver ocupada.

    Idempotente por `origem`: se a mesma chamada de tool for reexecutada, devolve o mesmo código.
    """
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existente = conn.execute(
                "SELECT codigo FROM reservas WHERE origem = ?", (origem,)
            ).fetchone()
            if existente:
                conn.execute("ROLLBACK")
                return existente["codigo"]
            codigo = _novo_codigo(conn)
            conn.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data, origem) VALUES (?, ?, ?, ?, ?)",
                (codigo, apartamento, area_id, data, origem),
            )
            conn.execute("COMMIT")
            return codigo
        except sqlite3.IntegrityError as erro:
            conn.execute("ROLLBACK")
            if "reservas.area" in str(erro) or "ux_reserva_ativa" in str(erro):
                raise ReservaIndisponivel() from erro
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise


def cancelar_reserva(apartamento: str, area_id: str, data: str) -> str | None:
    """Cancela a reserva ativa do PRÓPRIO apartamento. Devolve o código cancelado ou None."""
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT codigo FROM reservas WHERE apartamento = ? AND area = ? AND data = ? AND status = 'ativa'",
            (apartamento, area_id, data),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        conn.execute("UPDATE reservas SET status = 'cancelada' WHERE codigo = ?", (row["codigo"],))
        conn.execute("COMMIT")
        return row["codigo"]


# ------------------------------------------------------------------ visitantes
def visitantes_do_apartamento(apartamento: str) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id", (apartamento,)
        ).fetchall()
    return [dict(r) for r in rows]


def autorizar_visitante(apartamento: str, nome: str, data: str, origem: str) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO visitantes (apartamento, nome, data, origem) VALUES (?, ?, ?, ?)",
            (apartamento, nome, data, origem),
        )
