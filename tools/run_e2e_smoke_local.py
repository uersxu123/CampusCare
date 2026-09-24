from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve(path: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    return path if path.is_absolute() else root / path


def _freeze(dataset: Path, safety: Path, audit_path: Path) -> dict:
    audit = json.loads(audit_path.read_text(encoding="utf-8-sig"))
    expected = audit.get("datasetSha256") or {}
    actual = {dataset.name: _sha256(dataset), safety.name: _sha256(safety)}
    if expected != actual:
        raise ValueError(f"冻结数据 hash 与审计不一致: expected={expected} actual={actual}")
    business_count = sum(bool(line.strip()) for line in dataset.read_text(encoding="utf-8-sig").splitlines())
    safety_count = sum(bool(line.strip()) for line in safety.read_text(encoding="utf-8-sig").splitlines())
    if (business_count, safety_count) != (13, 3):
        raise ValueError(f"新版 Smoke 必须为 13+3，实际为 {business_count}+{safety_count}")
    return {"datasetSha256": actual, "businessCaseCount": business_count, "safetyCaseCount": safety_count}


def _write_markdown(run_dir: Path, exit_code: int) -> None:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    report = json.loads((run_dir / "e2e-report.json").read_text(encoding="utf-8"))
    metrics = report.get("metrics") or {}
    execution = report.get("execution") or {}
    errors = report.get("metricErrors") or []
    lines = [
        "# E2E Smoke V2 评测报告",
        "",
        f"- Run ID：`{summary.get('runId')}`",
        f"- 创建时间：`{summary.get('createdAt')}`",
        "- 数据集：冻结的 13 条业务 + 3 条安全题",
        "- Judge：本地 `qwen3:8b`（Ollama 原生 API，沿用项目上下文和 think 配置）",
        "- 隔离模式：每 case 独立 MCP runtime",
        "- Redis：本轮评测隔离显式禁用短期记忆与 Turn snapshot",
        "- hybrid 校验：启用",
        "- 重跑策略：本轮只执行一次，不重跑失败样本",
        f"- 质量门禁结果：`{'PASS' if summary.get('passed') else 'FAIL'}`",
        f"- 进程退出码：`{exit_code}`（使用 `--no-gate` 时质量失败不改变为基础设施失败）",
        "",
        "## 执行与观测指标",
        "",
        f"- planned / executed / notRun：{execution.get('planned')} / {execution.get('executed')} / {execution.get('notRun')}",
        f"- routeObservedAccuracy：{metrics.get('routeObservedAccuracy')}",
        f"- routeObservationCoverage：{metrics.get('routeObservationCoverage')}",
        f"- actionObservedAccuracy：{metrics.get('actionObservedAccuracy')}",
        f"- toolDecisionObservedAccuracy：{metrics.get('toolDecisionObservedAccuracy')}",
        f"- toolExecutionSuccessRate：{metrics.get('toolExecutionSuccessRate')}",
        f"- infrastructureFailureRate：{metrics.get('infrastructureFailureRate')}",
        f"- judgeCoverage / judgeScorableCoverage：{metrics.get('judgeCoverage')} / {metrics.get('judgeScorableCoverage')}",
        f"- endToEndCasePassRate：{metrics.get('endToEndCasePassRate')}",
        f"- actionObservationCoverage：{metrics.get('actionObservationCoverage')}",
        f"- retrievalObservationCoverage：{metrics.get('retrievalObservationCoverage')}",
        f"- rerankSuccessRate：{metrics.get('rerankSuccessRate')}",
        "",
        "## 业务评分（有效 Judge 输出的平均分）",
        "",
        "| 指标 | 均值 | 有效分母 |",
        "|---|---:|---:|",
        *[f"| {name} | {metrics.get(name)} | {(report.get('denominators') or {}).get(name)} |"
          for name in ("relevance", "accuracy", "completeness", "helpfulness", "action_correctness")],
        "",
        "正文动作由 Judge 从实际回答识别，运行状态只作独立遥测；未识别动作不按拒答计分。",
        "逐题通过同时检查 Judge、允许动作、工具合同和路由；Judge 矛盾输出不纳入质量均分。",
        "新版契约与历史版本不同，分数变化不能全部归因于模型能力变化。",
        "",
        "## 失败与限制",
        "",
    ]
    if errors:
        lines.extend(f"- `{item.get('caseId', 'run')}`：`{item.get('errorCode')}`" for item in errors)
    else:
        lines.append("- 无运行或评分错误。")
    lines.extend([
        "",
        "原始结果见同目录 `summary.json`、`e2e-report.json` 和 `cases.jsonl`。本报告不追改 Gold，不把未评分样本视为通过。",
        "",
    ])
    (run_dir / "E2E_SMOKE_V2_REPORT.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="冻结并仅运行一次 16 条本地 E2E Smoke V2")
    parser.add_argument("--judge-model", default="qwen3:8b")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--safety-dataset", type=Path, required=True)
    parser.add_argument("--corpus-audit", type=Path, required=True)
    parser.add_argument("--isolation-mode", choices=("isolated",), default="isolated")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-gate", action="store_true")
    args = parser.parse_args()
    dataset, safety, audit = map(_resolve, (args.dataset, args.safety_dataset, args.corpus_audit))
    output = _resolve(args.output)
    output.mkdir(parents=True, exist_ok=True)
    marker = output / ".single-smoke-v2-started.json"
    if marker.exists():
        raise RuntimeError(f"正式 16 条 Smoke 已启动过，禁止重跑: {marker}")
    frozen = _freeze(dataset, safety, audit)
    marker.write_text(json.dumps({
        "startedAt": datetime.now(UTC).isoformat(),
        "judgeModel": args.judge_model,
        "isolationMode": args.isolation_mode,
        **frozen,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    os.environ.update({
        "EVAL_JUDGE_PROVIDER": "ollama",
        "EVAL_JUDGE_BASE_URL": "http://127.0.0.1:11434",
        "EVAL_JUDGE_API_KEY": "ollama-local",
        "EVAL_JUDGE_MODEL": args.judge_model,
        "EVAL_JUDGE_MAX_RETRIES": "0",
        "EVAL_JUDGE_REPETITIONS": "1",
        "EVAL_PROFILE": "full",
        "EVAL_REQUIRE_HYBRID_RETRIEVAL": "true",
        "EVAL_ISOLATION_MODE": args.isolation_mode,
        "CHAT_TOOLS_STRICT_STARTUP": "true",
    })
    from app.evaluation.runner import main as run_evaluation

    before = {path.resolve() for path in output.iterdir() if path.is_dir()}
    run_args = [
        "--suite", "e2e", "--profile", "full",
        "--dataset", str(dataset),
        "--safety-dataset", str(safety),
        "--corpus-audit", str(audit),
        "--isolation-mode", args.isolation_mode,
        "--output", str(output),
    ]
    if args.no_gate:
        run_args.append("--no-gate")
    exit_code = run_evaluation(run_args)
    created = [path for path in output.iterdir() if path.is_dir() and path.resolve() not in before]
    if len(created) != 1:
        raise RuntimeError(f"无法唯一定位正式 Smoke 输出目录: {created}")
    run_dir = created[0]
    _write_markdown(run_dir, exit_code)
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    marker_payload.update({"completedAt": datetime.now(UTC).isoformat(), "exitCode": exit_code, "runDirectory": str(run_dir)})
    marker.write_text(json.dumps(marker_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(run_dir)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
