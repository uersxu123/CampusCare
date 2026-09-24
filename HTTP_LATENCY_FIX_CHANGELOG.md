# Ollama Local HTTP Latency Fix

## Changes

1. Local-host defaults now use `http://127.0.0.1:11434` instead of `http://localhost:11434` for host-native execution, avoiding Windows `::1` connection-fallback delay when Ollama is only listening on IPv4.
2. Synchronous Ollama calls in `app/services/ai.py` now reuse a process-level `httpx.Client` per normalized Ollama endpoint.
3. Docker behavior is intentionally unchanged: `docker-compose.yml` continues to use `host.docker.internal` because `127.0.0.1` inside a container refers to the container itself.
4. Benchmark/tuning/dev-script local defaults were aligned to `127.0.0.1`.
5. Added `tools/benchmark_planner_http_ablation_20.py` for a controlled 20-case HTTP ablation.

## Not changed

- Planner prompt / schema
- Fast Route rules, thresholds, prototypes, or routing policy
- RoutePlan contract
- RAG / Specialist / ResponseAgent
- Model, temperature, think mode, or context size
- Async streaming client lifecycle (not on the evaluated Planner route)

## Validation

Offline unit/contract tests run with in-memory SQLite:

```text
12 passed
```

The actual Ollama performance numbers must be re-measured on the user's Windows host.
