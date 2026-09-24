# Fast Route threshold update: 0.92 / 0.90 / 0.08

- `route_fast_min_score`: 0.95 -> 0.92
- `route_fast_competing_score`: 0.90 (unchanged)
- `route_fast_margin`: 0.08 (unchanged)

Rationale: previous real bge-m3 calibration reported 0.92/0.90/0.08 at 98.11% Fast Precision and 33.12% Coverage; old hold-out gate replay reported 98.28% and 27.88%. The original 0.95 setting only produced 8.17% Fast Coverage in the formal A/B. Multi-goal/context Pre-Gate remains mandatory; lowering the score threshold is not a replacement for structural guarding.

Validation rule: tune only on the new traffic calibration split; lock parameters before the traffic hold-out and continue to run the legacy 208 stress set.
