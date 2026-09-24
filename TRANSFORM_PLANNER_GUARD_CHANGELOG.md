# Transform Planner Guard 变更说明

## 目标

移除上一版 `Pure Transform -> 直接 CHAT` 的前置业务分类逻辑。前置 Gate 只负责判断请求是否适合进入 Rule + Embedding Fast Route，不直接决定文本变换请求的最终 Intent。

## 最终链路

1. 前置结构 Gate：识别多目标、上下文依赖、强依赖及文本变换风险结构。
2. 命中上述风险结构：跳过 Rule / Embedding，直接升级 LLM Planner。
3. 未命中：进入 Rule + Embedding + Confidence Gate。
4. 高置信单域请求 Fast Direct；其余进入 LLM Planner。

## 本次代码修改

- `app/services/routing_v5.py`
  - 删除 `is_pure_transform_request()`。
  - 删除 `PURE_TRANSFORM_CHAT` Fast Direct override。
  - 文本翻译、改写、润色、标题/格式转换等结构仅作为 `requires_planner()` 的 Planner-required 信号。
  - 引号中的领域词不再用于该结构信号本身的判断。
- `app/agents/routing.py`
  - 删除 `FAST_TRANSFORM_CHAT` 直接构造 CHAT RoutePlan 的分支。
  - 命中文本变换风险结构时不创建 `PrimaryRoutingScorer`，不调用 embedding，直接进入 Planner。
- `app/services/route_planning.py`
  - `PlanSource` 删除 `FAST_TRANSFORM_CHAT`，恢复为 `FAST_RULE_EMBEDDING` 与 Planner/降级来源。
- `app/evaluation/evaluators/routing.py`、`tools/benchmark_fast_route_v55.py`
  - Fast Route 统计只把 `FAST_RULE_EMBEDDING` 计为 Fast Direct。
- `app/services/intent_prompts.py`
  - 保留文本变换边界说明：纯翻译/改写/润色等最终应由 Planner 判定为 CHAT；若需要先做政策判断、资格核验或专业分析，则按实际业务 Intent 拆分。

## 两个原 false-fast 的新行为

- `把“我最近失眠睡不着”改写得更正式。`
  - 前置 Gate：Planner-required
  - Rule/Embedding：不调用
  - 最终 Intent：由 Planner 判断，预期 CHAT
- `把“宿舍换寝申请”改成一个邮件标题。`
  - 前置 Gate：Planner-required
  - Rule/Embedding：不调用
  - 最终 Intent：由 Planner 判断，预期 CHAT

## 回归

本地不依赖 Ollama 的路由回归：
- `tests/test_routing_v5_fast_path.py`: 16 passed
- `tests/test_routing_v5.py`: 9 passed
- `tests/test_routing_v5_runtime.py`: 3 passed
- `tests/test_route_planning.py`: 6 passed

合计 34 个相关测试通过。

真实 Ollama 的 240 Calibration / 360 Holdout / 三轮 A/B 需要在用户本机环境重新执行，不能用本地单元测试代替。
