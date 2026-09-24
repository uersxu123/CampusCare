from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module

from app.evaluation.config import EvaluationSettings


class RagasDependencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RagasFactories:
    llm: object
    embeddings: object
    version: str


def require_ragas() -> str:
    try:
        ragas = import_module("ragas")
        import_module("ragas.metrics.collections")
    except ImportError as exc:
        raise RagasDependencyError(
            "缺少 RAGAS 评测依赖，请安装 requirements-evaluation.txt（要求 ragas==0.4.3）"
        ) from exc
    version = str(getattr(ragas, "__version__", "unknown"))
    if version != "0.4.3":
        raise RagasDependencyError(f"RAGAS 版本不兼容：期望 0.4.3，实际 {version}")
    return version


def create_factories(settings: EvaluationSettings) -> RagasFactories:
    version = require_ragas()
    if not settings.judge_api_key.get_secret_value():
        raise RagasDependencyError("EVAL_JUDGE_API_KEY 为空，RAGAS 必须 fail closed")
    try:
        llms = import_module("ragas.llms")
        embeddings = import_module("ragas.embeddings")
        # LiteLLM ships a local cl100k_base cache, but only initializes it when
        # this module is imported before the first embedding request.
        import_module("litellm.litellm_core_utils.default_encoding")
        cache_module = import_module("ragas.cache")
        openai = import_module("openai")
        cache_dir = settings.resolve(settings.ragas_cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache = cache_module.DiskCacheBackend(str(cache_dir))
        llm_client = openai.AsyncOpenAI(
            api_key=settings.judge_api_key.get_secret_value(),
            base_url=settings.normalized_judge_base_url,
            timeout=settings.judge_timeout_seconds,
            max_retries=settings.judge_max_retries,
        )
        llm = llms.llm_factory(
            model=settings.judge_model,
            provider="openai",
            client=llm_client,
            cache=cache,
            temperature=settings.judge_temperature,
            max_tokens=settings.ragas_judge_max_tokens,
        )
        if settings.ragas_embedding_provider.lower() == "ollama":
            embedding = embeddings.LiteLLMEmbeddings(
                model=f"ollama/{settings.ragas_embedding_model}",
                api_base=settings.ragas_embedding_base_url.rstrip("/"),
                timeout=int(settings.judge_timeout_seconds),
                max_retries=settings.judge_max_retries,
                cache=cache,
            )
        else:
            embedding = embeddings.embedding_factory(
                provider=settings.ragas_embedding_provider,
                model=settings.ragas_embedding_model,
                base_url=settings.ragas_embedding_base_url.rstrip("/"),
                interface="modern",
                cache=cache,
            )
    except Exception as exc:
        raise RagasDependencyError(f"RAGAS Judge 或 embedding factory 构造失败: {type(exc).__name__}") from exc
    return RagasFactories(llm=llm, embeddings=embedding, version=version)
