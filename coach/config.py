import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")

RIOT_API_KEY = os.getenv("RIOT_API_KEY", "")
RIOT_ID = os.getenv("RIOT_ID", "")
PLATFORM = os.getenv("PLATFORM", "euw1")
REGION = os.getenv("REGION", "europe")

LOL_PATH = os.getenv("LOL_PATH", r"C:\Riot Games\League of Legends")
PORT = int(os.getenv("PORT", "8765"))

DB_PATH = DATA_DIR / "coach.db"

# Ranked Solo/Duo e Flex
RANKED_QUEUES = (420, 440)
POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")

# Início da season atual (para o histórico). Muda no .env se a data estiver errada.
SEASON_START = os.getenv("SEASON_START", "2026-01-08")

# Regiões disponíveis para recolher dados meta: plataforma -> routing regional
META_PLATFORMS = {"euw1": "europe", "kr": "asia", "eun1": "europe", "na1": "americas"}
