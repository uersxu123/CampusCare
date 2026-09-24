from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.mcp_client import McpToolError, MindBridgeMcpToolClient
from app.services.tool_executor import CircuitState


def _tool(name: str, identifier: str):
    return SimpleNamespace(
        name=name,
        inputSchema={
            "type": "object",
            "properties": {
                identifier: {"type": "integer", "minimum": 1},
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "required": [identifier, "idempotency_key"],
            "additionalProperties": False,
        },
    )


TOOLS = [
    _tool("mindbridge_excel_report", "report_id"),
    _tool("mindbridge_case_create", "report_id"),
    _tool("mindbridge_alert_send", "case_id"),
]


class FakeRiskSession:
    def __init__(self, *, timeout: bool = False):
        self.timeout = timeout
        self.calls = []
        self.list_calls = 0

    async def list_tools(self):
        self.list_calls += 1
        return SimpleNamespace(tools=TOOLS)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.timeout:
            import asyncio

            await asyncio.sleep(0.05)
        if name == "mindbridge_case_create":
            text = "success: caseId=9, reportId=7, status=OPEN"
        else:
            text = "success"
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text=text)])


def _attach_session(client: MindBridgeMcpToolClient, session: FakeRiskSession) -> None:
    @asynccontextmanager
    async def session_context():
        yield session

    client._session = session_context


@pytest.mark.asyncio
async def test_risk_mcp_uses_schema_idempotency_keys_and_never_caches_writes() -> None:
    session = FakeRiskSession()
    client = MindBridgeMcpToolClient(Settings(_env_file=None))
    _attach_session(client, session)

    await client.handle_report(7, "HIGH")
    await client.handle_report(7, "HIGH")

    assert session.list_calls == 2
    assert len(session.calls) == 6
    assert session.calls[0][1] == {"report_id": 7, "idempotency_key": "report:7:excel"}
    assert session.calls[2][1] == {"case_id": 9, "idempotency_key": "report:7:alert"}
    assert not hasattr(client, "cache")


@pytest.mark.asyncio
async def test_risk_mcp_timeout_opens_only_its_private_circuit_and_falls_back_deterministically() -> None:
    session = FakeRiskSession(timeout=True)
    client = MindBridgeMcpToolClient(Settings(
        _env_file=None,
        risk_mcp_call_timeout_seconds=0.001,
        risk_mcp_circuit_failure_threshold=1,
        risk_mcp_circuit_recovery_seconds=60,
    ))
    _attach_session(client, session)

    with pytest.raises(McpToolError, match="TIMEOUT"):
        await client.handle_report(7, "LOW")
    assert client.circuit_for("mindbridge_excel_report").state == CircuitState.OPEN
    assert client.circuit_for("mindbridge_case_create").state == CircuitState.CLOSED

    with pytest.raises(McpToolError, match="CIRCUIT_OPEN"):
        await client.handle_report(7, "LOW")
    assert len(session.calls) == 1
