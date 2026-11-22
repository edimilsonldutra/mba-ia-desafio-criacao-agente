"""Assistente virtual do Residencial Aurora (Google ADK + FastAPI)."""
import logging


def main() -> None:
    """`uv run aurora-api`: sobe a API em http://localhost:8000."""
    import uvicorn

    from . import config

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("residencial_aurora.api:app", host=config.HOST, port=config.PORT)


def restaurar() -> None:
    """`uv run aurora-restore`: volta reservas e visitantes aos dados iniciais e apaga as sessões."""
    from . import config, storage

    storage.restaurar_dados_iniciais()
    if config.SESSOES_DB.exists():
        config.SESSOES_DB.unlink()
    print("Dados restaurados a partir de dados/*.json; sessões apagadas.")
