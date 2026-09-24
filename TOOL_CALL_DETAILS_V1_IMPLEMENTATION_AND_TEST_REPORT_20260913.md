# 工具调用明细 V1 实现与测试记录

日期：2026-09-13

## 实现范围

在 AgentLoop 采集逐次调用，记录工具名、模型调用 ID、模型轮次、参数摘要、成功/失败/超时/拒绝/未执行、错误摘要、缓存命中、降级、开始时间、应用侧等待耗时和 RAG 重排状态。

参数摘要递归处理敏感字段，遮盖常见令牌、邮箱和手机号格式，并限制深度、条数和长度；不承诺识别自由文本中的所有个人信息。原有 Trace 对原始 arguments 的屏蔽规则保留，新增字段名为 argumentsSummary。

AgentLoopResult 增加默认空的 call_details；专业 Agent 把它保存到本轮 AgentRuntimeServices 的独立诊断集合。在所有模型调用结束后，运行时才将其合并到已有 tool_diagnostics。明细不进入 specialist_result、工具消息、Response 或 Safety 模型输入。

复用 AgentRunTrace.tool_diagnostics_json，无新增表或数据库迁移。现有管理接口 GET /api/admin/agent-traces 的 toolDiagnostics.workItems[].toolSummary.calls 返回明细，没有新增页面或按 Request ID 筛选接口。

请求关联沿用 ChatTurn 的 user_id + request_id → trace_id → AgentRunTrace；Request ID 不是全局唯一标识，排查时需同时确认用户或内部 Turn ID。

## 文件

新增：app/services/tool_call_details.py、tests/test_tool_call_details.py。

修改：app/services/agent_loop.py、app/services/tool_models.py、app/agents/autonomous.py、app/agents/event_driven_runtime.py、app/services/trace.py。

未修改工具执行器、缓存/熔断/fallback、提示词、权限、澄清、循环预算、RAG 算法、数据库模型或六个预留工具的关闭状态。

## 字段解释和边界

- durationMs：应用侧调用计时，包含等待执行器返回，不是远端服务器独立处理耗时；超时不代表后台操作已取消。
- status=NOT_EXECUTED：被调用预算或重复调用规则等拦截；不能把明细条数当作实际执行次数，原 callCount 保持原有语义。
- cached=true：本次工具结果缓存命中。
- rerankStatus=NOT_EXECUTED_CACHED：本次没有重新重排；SUCCEEDED/FAILED/SKIPPED 从既有 RAG 诊断推导，缺少依据标记 UNKNOWN；非 RAG 为 NOT_APPLICABLE。
- 降级状态记录 ToolResult.degraded，不增加实际 fallback 次数和内部重试明细。
- 随本轮现有 Trace 保存；模型失败或预算终止等正常返回路径保留已采集明细。进程崩溃、未被现有运行时收敛的异常或落库前退出，仍可能丢失本轮记录，不是逐次实时审计。
- 不采集完整工具结果正文，不把明细额外发送给模型。

## 验证

运行：

```text
python -m pytest tests/test_tool_call_details.py tests/test_tool_calling_v2.py tests/test_specialist_agents_v2.py tests/test_event_driven_multi_agent.py tests/test_response_agent_direct_generation_v3.py tests/test_specialist_rag_gate_prompt.py tests/test_context_builder.py tests/test_mcp_tools_v2.py tests/test_risk_mcp_isolation_v2.py tests/test_extension_tools.py tests/test_turn_execution_no_second_response_model_v3.py tests/test_clarification_study_plan_flow.py -q --tb=short -rs
```

结果：116 passed，1 skipped。天气联网集成测试因原有 WEATHER_INTEGRATION_TEST_ENABLED 未开启而跳过。

新增测试覆盖：成功、缓存、超时、错误、参数拒绝、权限拒绝、模型失败、不完整结束、总期限、模型/调用/结果预算、重复调用、一次 RAG 限制、重排状态、参数脱敏与长度、模型消息无额外诊断、不同轮次记录隔离、正常 save_run 落库和 finalize_generation_trace 落库。

使用本地 SQLite 验证相同 SQLAlchemy ORM/Trace 序列化路径，并关闭连接、重新打开数据库，通过 user_id/request_id 关联读取明细。本次未连接真实 MySQL；生产仍使用原项目数据库配置。

额外运行 tests/test_chat_completion.py 和 tests/test_chat_turns.py 时有 8 项失败、8 项通过。使用修改前保存的五个原始模块复测，结果同为 8 项失败、8 项通过；断言涉及旧版续写/STOP 行为，与当前 DIRECT_RESPONSE 流程不一致。本次未修改这些既有测试或业务流程来消除失败。

## 简历建议

补充 MCP 工具调用可观测性，采集参数摘要、执行状态、缓存命中、耗时及重排诊断，复用 MySQL Trace 持久化并关联用户请求，支持工作项级调用追踪与故障排查。

不写“全链路分布式追踪”“逐次实时持久化”或“完整原始参数审计”，本轮未实现这些能力。
