# Planner Guard 本地回归报告

## 结论

本次修改移除了 `Pure Transform -> 直接 CHAT` 的前置业务分类。文本变换类风险请求现在只由前置结构 Gate 判定为“不适合 Fast Route”，随后直接进入 LLM Planner。

## 静态行为核对

- `把“我最近失眠睡不着”改写得更正式。` -> `requires_planner=True`
- `把“宿舍换寝申请”改成一个邮件标题。` -> `requires_planner=True`
- 上述请求在生产 `classify_route()` 中不会创建 Fast scorer，因此不会调用 embedding。
- 普通简单请求如 `国家助学金申请条件是什么？`、`申请换宿舍` 仍可进入 Rule + Embedding Fast Route。
- Planner Prompt 保留文本变换边界，纯文本变换最终预期为 CHAT；知识依赖型生成由 Planner 按真实业务目标处理。

## 本地单元测试

执行：

```bash
DATABASE_URL='sqlite+pysqlite:///:memory:' PYTHONPATH=. pytest -q \
  tests/test_routing_v5_fast_path.py \
  tests/test_routing_v5.py \
  tests/test_routing_v5_runtime.py \
  tests/test_route_planning.py
```

结果：`34 passed`。

## 尚未替代的真实评测

当前环境无法访问用户本机 Ollama，因此以下结果必须在用户环境重新实测：
- 240 Calibration
- 360 Holdout
- Holdout 三轮真实端到端 A/B
- 旧 208 stress 回归

不得用本地单测推断 Fast Precision、Coverage 或端到端 latency。
