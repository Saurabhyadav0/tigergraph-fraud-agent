import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    graph_backend: str = os.getenv("GRAPH_BACKEND", "mock")  # "mock" | "tigergraph"
    tg_host: str = os.getenv("TG_HOST", "")
    tg_username: str = os.getenv("TG_USERNAME", "")
    tg_password: str = os.getenv("TG_PASSWORD", "")
    tg_graph_name: str = os.getenv("TG_GRAPH_NAME", "FraudGraph")
    tg_secret: str = os.getenv("TG_SECRET", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    data_dir: str = os.getenv("DATA_DIR", "data/sample")
    real_data_dir: str = os.getenv("REAL_DATA_DIR", "data/HHGOA_IEEE")
    outputs_dir: str = os.getenv("OUTPUTS_DIR", "outputs/cases")


CONFIG = Config()
