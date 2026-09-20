from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str

    db_pool_size: int = 15
    db_max_overflow: int = 0
    db_pool_min_conn: int = 15
    db_pool_timeout_s: float = 0.05
    anyio_thread_tokens: int = Field(default=40, ge=1)

    presupuesto_transferencia_s: float = 1.8
    max_reintentos: int = 3




settings = Settings()
