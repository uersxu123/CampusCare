import math
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.enums import MAX_WORK_ITEMS


class Settings(BaseSettings):
    agent_framework: str = "event_driven_multi_agent"
    agent_max_rounds: int = 12
    agent_max_rounds_hard_limit: int = 16
    agent_max_claims_per_round: int = 4
    agent_max_claims_per_agent: int = 4
    agent_final_acceptance_min_confidence: float = 0.6
    agent_max_response_revisions: int = 1
    agent_model_default_provider: str = ""
    agent_model_default_model: str = ""
    agent_model_coordinator_provider: str = ""
    agent_model_coordinator_model: str = ""
    agent_model_understanding_provider: str = ""
    agent_model_understanding_model: str = ""
    agent_model_safety_provider: str = ""
    agent_model_safety_model: str = ""
    agent_model_specialist_provider: str = "ollama"
    agent_model_specialist_model: str = "qwen3:8b"
    agent_model_response_provider: str = ""
    agent_model_response_model: str = ""
    agent_model_coordinator_temperature: float = 0.1
    agent_model_coordinator_max_tokens: int = 384
    agent_model_coordinator_think: bool = False
    agent_model_understanding_temperature: float = 0.1
    agent_model_understanding_max_tokens: int = 768
    agent_model_understanding_think: bool = False
    agent_model_safety_temperature: float = 0.1
    agent_model_safety_max_tokens: int = 384
    agent_model_safety_think: bool = False
    agent_model_specialist_temperature: float = 0.1
    agent_model_specialist_max_tokens: int = 1024
    agent_model_specialist_think: bool = False
    agent_model_response_temperature: float = 0.25
    agent_model_response_max_tokens: int = 1536
    agent_model_response_think: bool = False
    ai_provider: str = "ollama"
    ai_temperature: float = 0.35
    ai_max_tokens: int = 512
    ai_think: bool = False
    ai_empty_output_retry_enabled: bool = True
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:8b"
    ollama_num_ctx: int = 32768
    finetuned_model_name: str = "mindbridge-qwen2.5-7b-ft:latest"
    finetuned_model_dir: str = "models/mindbridge-qwen2.5-7b-ft"
    finetuned_model_file: str = "mindbridge-qwen2.5-7b-ft-q4_k_m.gguf"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_stream_include_usage: bool = True
    openai_embedding_model: str = "text-embedding-3-small"
    ollama_embedding_model: str = ""
    database_url: str = "mysql+pymysql://mindbridge:mindbridge@127.0.0.1:13306/mindbridge?charset=utf8mb4"
    knowledge_top_k: int = 4
    knowledge_max_evidence: int = 12
    knowledge_max_evidence_per_document: int = 3
    knowledge_search_timeout_seconds: float = 4.0
    clarification_slot_extractor_enabled: bool = True
    clarification_llm_fallback_enabled: bool = True
    knowledge_freshness_max_age_days: int = 365
    knowledge_min_evidence_score: float = 0.45
    knowledge_candidate_k: int = 48
    knowledge_lexical_candidate_k: int = 48
    knowledge_vector_candidate_k: int = 48
    knowledge_chunk_size: int = 512
    knowledge_chunk_overlap: int = 64
    knowledge_legacy_markdown_bootstrap_enabled: bool = False
    knowledge_artifact_dir: str = "data/knowledge-artifacts"
    knowledge_upload_max_bytes: int = 20 * 1024 * 1024
    knowledge_ingestion_worker_enabled: bool = True
    knowledge_ingestion_poll_interval_seconds: float = 1.0
    knowledge_ingestion_batch_size: int = 2
    knowledge_ingestion_max_attempts: int = 3
    knowledge_chunking_profile: str = "structure_token_v2"
    knowledge_child_target_tokens: int = 320
    knowledge_child_max_tokens: int = 480
    knowledge_child_min_tokens: int = 80
    knowledge_parent_max_tokens: int = 1200
    knowledge_overlap_tokens: int = 24
    knowledge_vector_enabled: bool = True
    knowledge_vector_required: bool = False
    knowledge_embedding_provider: str = "ollama"
    knowledge_embedding_model: str = "bge-m3:latest"
    knowledge_embedding_base_url: str = "http://127.0.0.1:11434"
    knowledge_vector_collection_base: str = "mindbridge_knowledge_v3"
    chroma_persist_dir: str = "data/chroma"
    chroma_snapshot_dir: str = "data/chroma-snapshots"
    chroma_snapshot_keep: int = 5
    embedding_timeout_seconds: float = 30.0
    skill_scenario_enabled: bool = True
    skill_rule_min_points: int = 80
    skill_rule_min_margin_points: int = 5
    skill_semantic_enabled: bool = True
    skill_semantic_min_similarity: float = 0.70
    skill_semantic_min_margin: float = 0.05
    skill_semantic_budget_ms: int = 3000
    skill_embedding_provider: str = ""
    skill_embedding_model: str = ""
    skill_embedding_base_url: str = ""
    skill_embedding_version: str = "v1"
    skill_max_matches: int = 2
    skill_total_chars: int = 5000
    skill_semantic_cache_max_items: int = 256
    agent_loop_max_model_rounds: int = 3
    agent_loop_max_tool_calls: int = 4
    agent_loop_max_result_chars: int = 12000
    agent_loop_deadline_seconds: float = 100.0
    turn_execution_deadline_seconds: float = 120.0
    chat_tools_enabled: bool = True
    chat_tools_stdio_command: str = ""
    chat_tools_stdio_args: str = "-m,app.chat_tools.server"
    chat_tools_schema_cache_seconds: float = 300.0
    chat_tools_connect_timeout_seconds: float = 5.0
    chat_tools_startup_timeout_seconds: float = 30.0
    chat_tools_cleanup_timeout_seconds: float = 5.0
    chat_tools_call_timeout_seconds: float = 15.0
    chat_tools_cache_max_items: int = 1000
    chat_tools_circuit_failure_threshold: int = 5
    chat_tools_circuit_recovery_seconds: float = 60.0
    weather_cache_ttl_seconds: float = 120.0
    weather_connect_timeout_seconds: float = 3.0
    weather_read_timeout_seconds: float = 5.0
    rag_model_provider: str = "ollama"
    rag_model: str = "qwen3:8b"
    rag_model_think: bool = False
    rag_model_max_tokens: int = 1024
    rag_rerank_temperature: float = 0.0
    rag_grade_temperature: float = 0.0
    rag_rewrite_temperature: float = 0.1
    rag_rerank_timeout_seconds: float = 40.0
    rag_grade_timeout_seconds: float = 8.0
    rag_rewrite_timeout_seconds: float = 6.0
    rag_tool_call_timeout_seconds: float = 65.0
    rag_cache_ttl_seconds: float = 300.0
    rag_max_retrieval_rounds: int = 2
    rag_max_rewrites: int = 1
    rag_retrieval_timeout_seconds: float = 8.0
    rag_pipeline_deadline_seconds: float = 60.0
    rag_per_list_candidate_k: int = 40
    rag_fused_candidate_limit: int = 24
    rag_grade_evidence_limit: int = 8
    rag_final_top_k: int = 5
    rag_tool_enabled: bool = True
    weather_tool_enabled: bool = True
    weather_geocoding_base_url: str = "https://geocoding-api.open-meteo.com/v1/search"
    weather_forecast_base_url: str = "https://api.open-meteo.com/v1/forecast"
    weather_http_timeout_seconds: float = 5.0
    clarification_max_no_progress: int = 3
    risk_mcp_call_timeout_seconds: float = 15.0
    risk_mcp_circuit_failure_threshold: int = 5
    risk_mcp_circuit_recovery_seconds: float = 60.0
    rag_eval_dataset: str = "app/rag_eval/mindbridge-rag-eval.json"
    rag_eval_output: str = "target/rag-eval-report.json"
    rag_eval_enabled: bool = False
    rag_eval_exit_after_run: bool = False
    excel_path: str = "data/mindbridge-risk-ledger.xlsx"
    redis_url: str = "redis://127.0.0.1:16379/0"
    redis_memory_enabled: bool = True
    redis_memory_ttl_seconds: int = 86400
    redis_memory_max_messages: int = 40
    redis_socket_timeout_seconds: float = 2.0
    context_input_max_tokens: int = 28672
    context_model_safety_margin_tokens: int = 1024
    context_compress_trigger_tokens: int = 24000
    context_compress_target_tokens: int = 20000
    context_reread_reserve_tokens: int = 3072
    tool_result_large_tokens: int = 8000
    tool_evidence_read_max_tokens: int = 1536
    tool_evidence_read_max_calls_per_agent: int = 2
    context_summary_max_calls_per_turn: int = 3
    context_summary_output_tokens: int = 1024
    context_summary_timeout_seconds: float = 10.0
    tool_result_recent_protected_groups: int = 2
    context_recent_message_limit: int = 8
    context_summary_max_tokens: int = 700
    context_user_memory_max_items: int = 5
    context_user_memory_max_tokens: int = 500
    context_knowledge_max_tokens: int = 2500
    intent_context_max_tokens: int = 1800
    route_planner_max_attempts: int = 2
    route_fast_enabled: bool = True
    route_fast_min_score: float = 0.92
    route_fast_competing_score: float = 0.90
    route_fast_margin: float = 0.08
    route_fast_embedding_timeout_seconds: float = 3.0
    route_fast_warmup_enabled: bool = True
    route_fast_warmup_timeout_seconds: float = 30.0
    route_degraded_rule_weight: float = 0.40
    route_degraded_embedding_weight: float = 0.60
    route_degraded_min_score: float = 0.90
    route_rule_only_min_score: float = 0.90
    context_safety_max_age_hours: int = 72
    trace_include_prompt_content: bool = False
    memory_v3_enabled: bool = True
    memory_episodic_enabled: bool = True
    memory_profile_extraction_enabled: bool = True
    memory_worker_enabled: bool = True
    memory_worker_poll_interval_seconds: float = 1.0
    memory_worker_batch_size: int = 4
    memory_worker_max_attempts: int = 5
    memory_worker_lease_seconds: int = 60
    memory_worker_retry_base_seconds: float = 5.0
    memory_chroma_mode: str = "persistent"
    memory_chroma_path: str = "data/memory-chroma"
    memory_chroma_host: str = "127.0.0.1"
    memory_chroma_port: int = 8000
    memory_embedding_provider: str = "ollama"
    memory_embedding_model: str = "bge-m3:latest"
    memory_embedding_base_url: str = "http://127.0.0.1:11434"
    memory_embedding_version: str = "bge-m3-v1"
    memory_history_candidate_k: int = 8
    memory_history_top_k: int = 3
    memory_history_max_tokens: int = 800
    memory_working_max_tokens: int = 2200
    memory_base_max_tokens: int = 4200
    memory_compaction_min_messages: int = 4
    memory_compaction_max_delta_tokens: int = 1500
    memory_idle_finalize_seconds: int = 1800
    memory_profile_min_confidence: float = 0.85
    memory_read_deadline_ms: int = 800
    memory_sql_fallback_candidate_limit: int = 50
    # Deprecated for one release cycle. The V2 production path reads CONTEXT_*.
    chat_history_limit: int = 10
    memory_compaction_enabled: bool = True
    memory_compaction_recent_messages: int = 8
    memory_summary_max_chars: int = 1200
    prompt_total_max_chars: int = 12000
    prompt_knowledge_max_chars: int = 5000
    app_timezone: str = "Asia/Shanghai"
    clarification_ttl_seconds: int = 86400
    clarification_max_rounds: int = 3
    chat_turn_snapshot_ttl_seconds: int = 1800
    chat_turn_snapshot_enabled: bool = True
    chat_turn_snapshot_interval_ms: int = 150
    chat_turn_snapshot_min_chars: int = 30
    chat_turn_poll_interval_ms: int = 150
    chat_turn_stale_seconds: int = 300
    chat_response_max_continuations: int = 1
    chat_response_max_total_tokens: int = 3072
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: float = 10.0
    alert_email_delivery_mode: str = "log"
    alert_email_from: str = ""
    alert_email_to: str = ""
    alert_email_subject_prefix: str = "[MindBridge 高风险预警]"
    tool_queue_enabled: bool = True
    tool_queue_poll_interval_seconds: float = 1.0
    tool_queue_batch_size: int = 10
    tool_queue_max_attempts: int = 3
    tool_queue_retry_delay_seconds: float = 15.0
    tool_queue_excel_workers: int = 1
    tool_queue_email_workers: int = 2
    alert_email_rate_limit_per_minute: int = 30

    @model_validator(mode="after")
    def validate_intent_fusion_settings(self) -> "Settings":
        if self.intent_context_max_tokens <= 0 or self.intent_context_max_tokens >= self.context_input_max_tokens:
            raise ValueError("intent_context_max_tokens must be positive and below context_input_max_tokens")
        if self.agent_max_claims_per_agent < MAX_WORK_ITEMS:
            raise ValueError("agent_max_claims_per_agent must cover four work items")
        if not 384 <= self.agent_model_understanding_max_tokens <= 1024:
            raise ValueError("agent_model_understanding_max_tokens must be in [384, 1024]")
        if self.agent_max_rounds < 12 or self.agent_max_rounds > self.agent_max_rounds_hard_limit:
            raise ValueError("agent_max_rounds must be at least 12 and not exceed the hard limit")
        if self.agent_max_response_revisions < 0 or self.agent_max_response_revisions > 2:
            raise ValueError("agent_max_response_revisions must be in [0, 2]")
        if self.memory_chroma_mode not in {"persistent", "http", "disabled"}:
            raise ValueError("memory_chroma_mode must be persistent, http, or disabled")
        if self.memory_base_max_tokens <= 0:
            raise ValueError("memory_base_max_tokens must be positive")
        if self.memory_history_top_k < 0 or self.memory_history_candidate_k < self.memory_history_top_k:
            raise ValueError("memory_history_candidate_k must cover memory_history_top_k")
        if not 0 <= self.skill_rule_min_points <= 100:
            raise ValueError("skill_rule_min_points must be in [0, 100]")
        if not 1 <= self.skill_rule_min_margin_points <= 100:
            raise ValueError("skill_rule_min_margin_points must be in [1, 100]")
        if not math.isfinite(self.skill_semantic_min_similarity) or not 0.0 <= self.skill_semantic_min_similarity <= 1.0:
            raise ValueError("skill_semantic_min_similarity must be in [0, 1]")
        if not math.isfinite(self.skill_semantic_min_margin) or not 0.0 < self.skill_semantic_min_margin <= 2.0:
            raise ValueError("skill_semantic_min_margin must be in (0, 2]")
        if self.skill_semantic_budget_ms < 0:
            raise ValueError("skill_semantic_budget_ms must be non-negative")
        if self.skill_max_matches < 0 or self.skill_total_chars < 0:
            raise ValueError("skill budgets must be non-negative")
        if self.skill_semantic_cache_max_items <= 0:
            raise ValueError("skill_semantic_cache_max_items must be positive")
        if not self.skill_embedding_version.strip():
            raise ValueError("skill_embedding_version must not be empty")
        if self.skill_embedding_provider.strip().lower() not in {"", "ollama", "openai", "disabled", "none"}:
            raise ValueError("skill_embedding_provider is unsupported")
        return self

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]


@lru_cache
def get_settings() -> Settings:
    return Settings()
