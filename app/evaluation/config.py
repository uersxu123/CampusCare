from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


ROUTING_PRIMARY_INTENT_MIN = 0.95
ROUTING_WORK_ITEM_COUNT_MIN = 0.95
ROUTING_ROUTE_PLAN_EXACT_MATCH_MIN = 0.95
ROUTING_CONTEXT_RELATION_MIN = 0.95
ROUTING_ORDER_ONLY_HARD_ERROR_MAX = 0.02
ROUTING_SINGLE_GOAL_NO_OVERSPLIT_MIN = 0.98


class EvaluationSettings(BaseSettings):
    enabled: bool = Field(False, validation_alias="EVAL_ENABLED")
    output_dir: Path = Field(Path("target/evaluation"), validation_alias="EVAL_OUTPUT_DIR")
    profile: str = Field("smoke", validation_alias="EVAL_PROFILE")
    dataset_dir: Path = Field(Path("app/evaluation/datasets"), validation_alias="EVAL_DATASET_DIR")
    corpus_audit: Path | None = Field(None, validation_alias="EVAL_CORPUS_AUDIT")
    isolation_mode: str = Field("isolated", validation_alias="EVAL_ISOLATION_MODE")
    storage_admin_url: SecretStr = Field(default_factory=lambda: SecretStr(""), validation_alias="EVAL_STORAGE_ADMIN_URL")
    baseline_dir: Path = Field(Path("target/evaluation/baselines"), validation_alias="EVAL_BASELINE_DIR")
    fail_on_nan: bool = Field(True, validation_alias="EVAL_FAIL_ON_NAN")
    max_workers: int = Field(2, ge=1, validation_alias="EVAL_MAX_WORKERS")
    case_timeout_seconds: float = Field(180.0, gt=0, validation_alias="EVAL_CASE_TIMEOUT_SECONDS")
    case_cancel_grace_seconds: float = Field(1.0, ge=0, validation_alias="EVAL_CASE_CANCEL_GRACE_SECONDS")
    case_cleanup_timeout_seconds: float = Field(5.0, gt=0, validation_alias="EVAL_CASE_CLEANUP_TIMEOUT_SECONDS")
    require_hybrid_retrieval: bool = Field(
        True,
        validation_alias="EVAL_REQUIRE_HYBRID_RETRIEVAL",
    )

    judge_provider: str = Field("openai", validation_alias="EVAL_JUDGE_PROVIDER")
    judge_base_url: str = Field("https://api.deepseek.com", validation_alias="EVAL_JUDGE_BASE_URL")
    judge_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), validation_alias="EVAL_JUDGE_API_KEY")
    judge_model: str = Field("deepseek-v4-flash", validation_alias="EVAL_JUDGE_MODEL")
    judge_temperature: float = Field(0.0, validation_alias="EVAL_JUDGE_TEMPERATURE")
    judge_max_tokens: int = Field(1200, ge=1, validation_alias="EVAL_JUDGE_MAX_TOKENS")
    judge_timeout_seconds: float = Field(120.0, gt=0, validation_alias="EVAL_JUDGE_TIMEOUT_SECONDS")
    judge_max_retries: int = Field(2, ge=0, validation_alias="EVAL_JUDGE_MAX_RETRIES")
    judge_repetitions: int = Field(1, ge=1, validation_alias="EVAL_JUDGE_REPETITIONS")
    judge_allow_prompt_fallback: bool = Field(True, validation_alias="EVAL_JUDGE_ALLOW_PROMPT_FALLBACK")

    ragas_enabled: bool = Field(True, validation_alias="RAGAS_ENABLED")
    ragas_profile: str = Field("smoke", validation_alias="RAGAS_PROFILE")
    # RAGAS faithfulness/factual-correctness use several structured calls and
    # need a larger completion budget than the compact business Judge.
    ragas_judge_max_tokens: int = Field(8192, ge=1, validation_alias="RAGAS_JUDGE_MAX_TOKENS")
    ragas_embedding_provider: str = Field("ollama", validation_alias="RAGAS_EMBEDDING_PROVIDER")
    ragas_embedding_base_url: str = Field(
        "http://127.0.0.1:11434", validation_alias="RAGAS_EMBEDDING_BASE_URL"
    )
    ragas_embedding_model: str = Field("bge-m3:latest", validation_alias="RAGAS_EMBEDDING_MODEL")
    ragas_cache_dir: Path = Field(
        Path("target/evaluation/cache/ragas"), validation_alias="RAGAS_CACHE_DIR"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @property
    def normalized_judge_base_url(self) -> str:
        return self.judge_base_url.rstrip("/")

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else self.project_root / path


@lru_cache
def get_evaluation_settings() -> EvaluationSettings:
    return EvaluationSettings()
