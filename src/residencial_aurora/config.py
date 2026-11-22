"""Configuração central: caminhos e variáveis de ambiente."""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

DADOS_DIR = PROJECT_ROOT / "dados"
VAR_DIR = Path(os.environ.get("AURORA_VAR_DIR", PROJECT_ROOT / "var"))

CONDOMINIO_DB = VAR_DIR / "condominio.db"
SESSOES_DB = VAR_DIR / "sessoes.db"

APP_NAME = "residencial_aurora"
MODEL = os.environ.get("AURORA_MODEL", "gemini-flash-lite-latest")
HOST = os.environ.get("AURORA_HOST", "127.0.0.1")
PORT = int(os.environ.get("AURORA_PORT", "8000"))
