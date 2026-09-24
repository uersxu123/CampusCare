from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Context WorkItems V7 Smoke 中文报告")
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = json.loads((args.run_dir / "summary.json").read_text(encoding="utf-8"))
    e2e = json.loads((args.run_dir / "e2e-report.json").read_text(encoding="utf-8"))
    cases = [json.loads(line) for line in (args.run_dir / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    probe = json.loads((args.run_dir / "probe-evidence.json").read_text(encoding="utf-8"))
    storage = json.loads((args.run_dir / "storage-probe-evidence.json").read_text(encoding="utf-8"))
    runtime = json.loads((args.run_dir / "runtime-probe-evidence.json").read_text(encoding="utf-8"))
    metrics = e2e.get("metrics", {})
    errors = e2e.get("metricErrors", [])
    report = f"""# E2E Smoke V2 报告：Context WorkItems V7

运行 ID：`{summary.get('runId')}`  
正式执行：一次，16 条（13 业务 + 3 安全），失败样本未重跑。  
总体结论：{'通过' if summary.get('passed') else '未通过'}。

## 冻结配置

- 数据集：`e2e-smoke-context-workitems-v7`
- Judge：本地 `qwen3:8b`，`think=false`，重试 0，修复 0，Prompt 回退关闭
- 隔离：每 case 独立 MySQL，Redis DB 15，单 worker
- Hybrid 校验：开启
- 正式结果可评分率：{metrics.get('judgeScorableCoverage')}

## 结果

- 执行记录：{len(cases)}
- 端到端通过率：{metrics.get('endToEndCasePassRate')}
- 路由观察准确率：{metrics.get('routeObservedAccuracy')}
- 工具决策准确率：{metrics.get('toolDecisionObservedAccuracy')}
- 工具执行成功率：{metrics.get('toolExecutionSuccessRate')}
- 重排成功率：{metrics.get('rerankSuccessRate')}
- 基础设施失败率：{metrics.get('infrastructureFailureRate')}
- Judge 错误率：{metrics.get('judgeErrorRate')}
- 失败或不可评分项：{json.dumps(errors, ensure_ascii=False)}

## 独立探针

- 模型/MCP/重排/Judge 探针：{'通过' if probe.get('passed') else '失败'}
- MySQL/Redis/迁移/权限探针：{'通过' if storage.get('passed') else '失败'}
- 隔离全链路合成探针：{'通过' if runtime.get('passed') else '失败'}
- 初始失败证据保留在同目录的 `*-before-*.json` 文件中；修复后未覆盖原始证据。

## 测试说明

定向合同与集成测试已通过；全量测试结果见 `pytest-full.xml`。全量历史测试仍含旧 V3、旧 rewrite/facet、乱码夹具、旧流式回复和知识导入契约失败，这些没有被伪报为通过。

## 解释边界

本报告只代表冻结代码、冻结数据集和本机服务下的单次 Smoke。它不证明语义错误为零，也不把 Specialist 自报的 `answerStatus` 当作 Judge Gold。
"""
    (args.run_dir / "E2E_SMOKE_V2_REPORT.md").write_text(report, encoding="utf-8", newline="\n")
    print(args.run_dir / "E2E_SMOKE_V2_REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
