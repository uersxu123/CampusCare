# Lightweight Structure Gate Change Log

## Goal
Keep the first-stage gate deterministic and cheap. It only answers whether a request is safe for Rule+Embedding Fast Route; it does not classify the final intent.

## Changes
1. Expanded explicit multi-goal markers with a few high-precision phrases: `有两个问题`, `有几个问题`, `这几个都`, `都帮我处理`.
2. Expanded Meta/Text-generation risk detection for requests such as writing an example email/template/reply/title. These are escalated to Planner instead of being classified by topic words in the payload.
3. Added lightweight punctuation clause splitting plus cross-domain hints:
   - split only on punctuation/newlines;
   - use a small high-precision hint table for ACADEMIC/CAMPUS/MENTAL;
   - only clauses with exactly one strong domain hint participate;
   - if separate clauses point to at least two different domains, escalate to Planner.
4. A comma alone never triggers Planner. Same-domain clauses remain eligible for Fast Route.
5. No LLM, embedding, tokenizer, parser, or small model is added to Structure Gate.

## Examples
- `实习求职怎么规划，我最近被拒很多次也很沮丧。` -> Planner (ACADEMIC hint + MENTAL hint across clauses)
- `我最近压力很大，晚上也睡不好。` -> not forced to Planner (MENTAL + MENTAL)
- `休学期间国家助学金还发吗？` -> not forced by clause hints (single clause)
- `帮我写一封询问宿舍调换的示例邮件。` -> Planner (Meta/Text-generation risk)

## Local regression
Run with SQLite to avoid external MySQL dependency during unit tests:

```bash
DATABASE_URL='sqlite+pysqlite:///:memory:' PYTHONPATH=. pytest -q \
  tests/test_routing_v5_fast_path.py \
  tests/test_routing_v5.py \
  tests/test_routing_v5_runtime.py \
  tests/test_route_planning.py
```

Result: `39 passed`.
