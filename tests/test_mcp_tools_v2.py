import os
import sys
import threading

import httpx
import pytest

from app.chat_tools.weather import OpenMeteoWeatherService, WeatherToolError
from app.services.mcp_runtime import McpRuntime, McpServerConfig
from app.services.tool_executor import CircuitBreaker, CircuitState, TTLCache, ToolExecutor, parse_call_tool_result
from app.services.tool_registry import ToolRegistry


TOOLS = [
    {
        "name": "rag_search",
        "description": "本地知识检索",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 2}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_current_weather",
        "description": "当前天气",
        "inputSchema": {
            "type": "object",
            "properties": {"location": {"type": "string", "minLength": 1, "maxLength": 80}},
            "required": ["location"],
            "additionalProperties": False,
        },
    },
]


def test_agent_tool_permission_matrix_is_static_and_exhaustive() -> None:
    registry = ToolRegistry()
    registry.register_tools("chat_readonly", TOOLS)

    assert [item.original_name for item in registry.for_agent("GeneralChatAgent")] == ["get_current_weather"]
    for name in ("AcademicPlanningAgent", "CampusAffairsAgent", "PsychologicalSupportAgent"):
        assert [item.original_name for item in registry.for_agent(name)] == ["rag_search"]
    for name in ("SafetyAgent", "ResponseAgent", "UnderstandingAgent", "RiskAgent"):
        assert registry.for_agent(name) == []


def test_invalid_schema_is_not_registered() -> None:
    registry = ToolRegistry()
    registry.register_tools("chat_readonly", [{
        "name": "broken",
        "description": "bad",
        "inputSchema": {"type": "not-a-json-schema-type"},
    }])

    assert registry.all_tools() == []
    assert registry.errors == [{"tool": "broken", "code": "INVALID_TOOL_SCHEMA"}]


class FakeRuntime:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def call_tool_sync(self, server, name, arguments, timeout):
        self.calls += 1
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_executor_validates_schema_and_caches_only_success() -> None:
    registry = ToolRegistry()
    registry.register_tools("chat_readonly", TOOLS, cache_ttls={"get_current_weather": 120})
    runtime = FakeRuntime([{"isError": False, "structuredContent": {"temperatureC": 31.0}}])
    executor = ToolExecutor(registry=registry, runtime=runtime)
    tool_name = registry.for_agent("GeneralChatAgent")[0].model_name

    invalid = executor.execute(
        agent_name="GeneralChatAgent",
        tool_name=tool_name,
        arguments={"location": "武汉", "url": "https://example.invalid"},
        remaining_seconds=10,
    )
    first = executor.execute(
        agent_name="GeneralChatAgent", tool_name=tool_name,
        arguments={"location": "武汉"}, remaining_seconds=10,
    )
    second = executor.execute(
        agent_name="GeneralChatAgent", tool_name=tool_name,
        arguments={"location": "武汉"}, remaining_seconds=10,
    )

    assert invalid.code == "INVALID_ARGUMENT"
    assert first.ok and not first.cached
    assert second.ok and second.cached
    assert runtime.calls == 1


def test_mcp_structured_error_mapping_never_guesses_from_text() -> None:
    unavailable = parse_call_tool_result({
        "isError": True,
        "structuredContent": {"error": {"code": "UPSTREAM_UNAVAILABLE", "message": "暂时不可用"}},
    })
    malformed = parse_call_tool_result({"isError": True, "content": [{"text": "timeout"}]})

    assert unavailable.code == "UPSTREAM_UNAVAILABLE"
    assert malformed.code == "MCP_PROTOCOL_ERROR"


def test_ttl_lru_and_three_state_circuit_breaker() -> None:
    cache = TTLCache(max_items=2)
    cache.put("a", 1, 60)
    cache.put("b", 2, 60)
    assert cache.get("a") == 1
    cache.put("c", 3, 60)
    assert cache.get("b") is None

    circuit = CircuitBreaker(failure_threshold=1, recovery_seconds=0)
    assert circuit.allow_call()
    circuit.record_failure()
    assert circuit.state == CircuitState.OPEN
    assert circuit.allow_call()
    assert circuit.state == CircuitState.HALF_OPEN
    assert not circuit.allow_call()
    circuit.record_success()
    assert circuit.state == CircuitState.CLOSED


def test_half_open_allows_only_one_concurrent_probe() -> None:
    circuit = CircuitBreaker(failure_threshold=1, recovery_seconds=0)
    circuit.record_failure()
    barrier = threading.Barrier(3)
    values = []

    def probe():
        barrier.wait()
        values.append(circuit.allow_call())

    threads = [threading.Thread(target=probe) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(values) == [False, True]


def _weather_transport(mode="success"):
    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if "geocoding" in request.url.host:
            if mode == "not-found":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(200, json={"results": [{
                "name": "武汉市", "country": "中国", "latitude": 30.58,
                "longitude": 114.27, "timezone": "Asia/Shanghai",
            }]})
        if mode == "unavailable":
            return httpx.Response(503, json={})
        return httpx.Response(200, json={
            "timezone": "Asia/Shanghai",
            "current": {
                "time": "2026-08-06T14:15", "temperature_2m": 31.2,
                "apparent_temperature": 35.0, "relative_humidity_2m": 68,
                "precipitation": 0.0, "weather_code": 2, "wind_speed_10m": 8.4,
            },
        })
    return httpx.MockTransport(handler)


def test_weather_mock_contract_and_wmo_mapping() -> None:
    result = OpenMeteoWeatherService(transport=_weather_transport()).get_current_weather(" 武汉 ")

    assert result == {
        "location": "武汉市", "country": "中国", "timezone": "Asia/Shanghai",
        "observedAt": "2026-08-06T14:15", "temperatureC": 31.2,
        "apparentTemperatureC": 35.0, "relativeHumidityPercent": 68,
        "precipitationMm": 0.0, "weatherCode": 2, "weatherText": "局部多云",
        "windSpeedKmh": 8.4, "source": "Open-Meteo",
    }


@pytest.mark.parametrize(
    ("mode", "code"),
    [("not-found", "LOCATION_NOT_FOUND"), ("timeout", "UPSTREAM_TIMEOUT"), ("unavailable", "UPSTREAM_UNAVAILABLE")],
)
def test_weather_error_classification(mode: str, code: str) -> None:
    with pytest.raises(WeatherToolError) as caught:
        OpenMeteoWeatherService(transport=_weather_transport(mode)).get_current_weather("武汉")
    assert caught.value.code == code


def test_real_stdio_server_lists_both_readonly_tools_with_strict_schema() -> None:
    runtime = McpRuntime([McpServerConfig("chat_readonly", sys.executable, ("-m", "app.chat_tools.server"))])
    try:
        tools = {tool.name: tool.inputSchema for tool in runtime.list_tools_sync("chat_readonly")}
    finally:
        runtime.shutdown()

    assert set(tools) == {"rag_search", "get_current_weather"}
    assert tools["get_current_weather"]["additionalProperties"] is False
    assert set(tools["get_current_weather"]["properties"]) == {"location"}


@pytest.mark.skipif(
    os.getenv("WEATHER_INTEGRATION_TEST_ENABLED", "false").lower() != "true",
    reason="WEATHER_INTEGRATION_TEST_ENABLED 未启用",
)
def test_open_meteo_integration_smoke() -> None:
    result = OpenMeteoWeatherService().get_current_weather("武汉")
    assert result["source"] == "Open-Meteo"
