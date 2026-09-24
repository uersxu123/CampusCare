from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "target/verification/context-workitems-followup-20260918"
FORMAL = ROOT / "target/evaluation/smoke-context-workitems-v7/20260917T200738Z-contextv7"
FILES = [
    "app/agents/autonomous.py", "app/agents/event_driven_runtime.py", "app/services/agent_loop.py",
    "app/services/tool_models.py", "app/services/execution_control.py", "app/services/ai.py", "app/services/trace.py",
    "app/evaluation/runtime/action_resolution.py", "app/evaluation/runner.py", "scripts/probe_context_workitems_v7.py",
    "scripts/probe_context_workitems_followup.py", "scripts/audit_context_workitems_followup.py",
    "tests/test_context_workitems_followup.py", "tests/test_agent_stop_reason_contract.py", "tests/test_tool_call_details.py",
    "tests/test_e2e_bugfix_contracts.py", "tests/test_three_layer_memory_v3.py",
    "tests/test_specialist_rag_gate_prompt.py",
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def suite(path):
    root = ET.parse(path).getroot()
    cases = root.findall(".//testcase")
    failures = {f"{c.get('classname')}::{c.get('name')}": c.find("failure").get("message", "")
                for c in cases if c.find("failure") is not None}
    skipped = sum(c.find("skipped") is not None for c in cases)
    errors = sum(c.find("error") is not None for c in cases)
    return {"total": len(cases), "passed": len(cases) - len(failures) - skipped - errors,
            "failed": len(failures), "skipped": skipped, "errors": errors, "failures": failures}


def class_ast(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return ast.dump(next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name), include_attributes=False)


def main():
    before = suite(OUT / "before-pytest.xml")
    after = suite(OUT / "pytest-final.xml")
    freeze = json.loads((FORMAL / "freeze-manifest.json").read_text(encoding="utf-8"))
    dataset = ROOT / "app/evaluation/datasets/e2e-smoke-context-workitems-v7"
    checks = {f"dataset:{name}": digest(dataset / name) == expected for name, expected in freeze["dataset"]["files"].items()}
    checks.update({f"protected:{name}": digest(ROOT / name) == freeze["runtimeFiles"][name]
                   for name in ("app/agents/routing.py", "app/services/routing_v5.py", "app/services/rag_pipeline.py")})
    checks["SafetyAgent AST unchanged"] = class_ast(ROOT / "app/agents/autonomous.py", "SafetyAgent") == class_ast(
        OUT / "before/app/agents/autonomous.py", "SafetyAgent")
    encoding = {}
    for name in FILES:
        raw = (ROOT / name).read_bytes()
        text = raw.decode("utf-8")
        encoding[name] = {"utf8WithoutBom": not raw.startswith(b"\xef\xbb\xbf"),
                          "unicodeEscapeMatches": re.findall(r"\\u[0-9a-fA-F]{4}", text)}
    probes = json.loads((OUT / "synthetic-agent-probes.json").read_text(encoding="utf-8"))
    report = {"before": before, "after": after,
              "newFailures": sorted(set(after["failures"]) - set(before["failures"])),
              "resolvedFailures": sorted(set(before["failures"]) - set(after["failures"])),
              "protectedChecks": checks, "encoding": encoding,
              "files": {name: digest(ROOT / name) for name in FILES},
              "syntheticProbePassed": sum(r["passed"] for r in probes["cases"]), "syntheticProbeCount": len(probes["cases"]),
              "formalSmokeRerun": False,
              "formalArtifacts": {name: digest(FORMAL / name) for name in ("summary.json", "cases.jsonl", "e2e-report.json", "freeze-manifest.json")}}
    (OUT / "verification-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"before": {k: v for k, v in before.items() if k != "failures"},
                      "after": {k: v for k, v in after.items() if k != "failures"},
                      "newFailures": report["newFailures"], "protected": checks,
                      "syntheticProbes": f"{report['syntheticProbePassed']}/{report['syntheticProbeCount']}"}, ensure_ascii=False))
    return 0 if not report["newFailures"] and all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
