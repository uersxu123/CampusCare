from __future__ import annotations

from importlib import import_module

from app.evaluation.ragas_eval.factories import RagasFactories, require_ragas


METRIC_CLASSES = {
    "faithfulness": "Faithfulness",
    "answerRelevancy": "AnswerRelevancy",
    "contextPrecision": "ContextPrecision",
    "contextRecall": "ContextRecall",
    "factualCorrectness": "FactualCorrectness",
}


def create_metrics(factories: RagasFactories) -> dict[str, object]:
    require_ragas()
    collections = import_module("ragas.metrics.collections")
    metrics = {}
    for name, class_name in METRIC_CLASSES.items():
        metric_type = getattr(collections, class_name, None)
        if metric_type is None:
            if name == "factualCorrectness":
                continue
            raise RuntimeError(f"RAGAS collections 缺少必做指标: {class_name}")
        kwargs = {}
        if name not in {"idBasedContextPrecision", "idBasedContextRecall"}:
            kwargs["llm"] = factories.llm
        if name == "answerRelevancy":
            kwargs["embeddings"] = factories.embeddings
        metrics[name] = metric_type(**kwargs)
    legacy = import_module("ragas.metrics")
    for name, class_name in (
        ("idBasedContextPrecision", "IDBasedContextPrecision"),
        ("idBasedContextRecall", "IDBasedContextRecall"),
    ):
        metric = getattr(legacy, class_name)()
        setattr(metric, "_mindbridge_legacy_single_turn", True)
        metrics[name] = metric
    return metrics
