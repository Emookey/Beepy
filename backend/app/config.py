from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "MBC Intelligence"
    database_url: str

    entra_client_id: str
    entra_tenant_id: str
    allowed_email_domain: str
    autotask_username: str
    autotask_secret: str
    autotask_integration_code: str
    autotask_base_url: str
    autotask_web_base_url: str = "https://ww14.autotask.net/Autotask/AutotaskExtend/ExecuteCommand.aspx"

    ollama_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "qwen3.5:9b"
    ollama_embed_model: str = "qwen3-embedding:0.6b"
    ollama_timeout_seconds: int = 240
    ollama_context: int = 32768

    sync_interval_seconds: int = 900
    full_sync_on_empty: bool = True
    max_autotask_pages: int = 2000
    sync_overlap_minutes: int = 15
    initial_full_sync: bool = True
    semantic_min_score: float = 0.28
    lexical_weight: float = 4.0
    semantic_weight: float = 1.0
    beepy_max_tickets: int = 25
    log_level: str = "INFO"

@lru_cache
def get_settings() -> Settings:
    return Settings()
