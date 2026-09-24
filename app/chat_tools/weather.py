from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


WMO_WEATHER_TEXT = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "轻微冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "轻微冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴轻微冰雹",
    99: "雷暴伴强冰雹",
}


@dataclass(frozen=True)
class WeatherToolError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


class OpenMeteoWeatherService:
    def __init__(
        self,
        *,
        geocoding_base_url: str = "https://geocoding-api.open-meteo.com/v1/search",
        forecast_base_url: str = "https://api.open-meteo.com/v1/forecast",
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.geocoding_base_url = geocoding_base_url
        self.forecast_base_url = forecast_base_url
        self.timeout_seconds = max(0.1, timeout_seconds)
        self.transport = transport

    def get_current_weather(self, location: str) -> dict[str, Any]:
        normalized = str(location or "").strip()
        if not normalized or len(normalized) > 80:
            raise WeatherToolError("INVALID_ARGUMENT", "location 必须为 1 到 80 个字符")
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                trust_env=False,
            ) as client:
                geocoding = client.get(self.geocoding_base_url, params={
                    "name": normalized,
                    "count": 1,
                    "language": "zh",
                    "format": "json",
                })
                geocoding.raise_for_status()
                geocoding_payload = geocoding.json()
                results = geocoding_payload.get("results") if isinstance(geocoding_payload, dict) else None
                if not isinstance(results, list) or not results:
                    raise WeatherToolError("LOCATION_NOT_FOUND", "没有找到该地点")
                place = results[0]
                latitude = _number(place, "latitude")
                longitude = _number(place, "longitude")
                forecast = client.get(self.forecast_base_url, params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": (
                        "temperature_2m,apparent_temperature,relative_humidity_2m,"
                        "precipitation,weather_code,wind_speed_10m"
                    ),
                    "timezone": "auto",
                })
                forecast.raise_for_status()
                forecast_payload = forecast.json()
        except WeatherToolError:
            raise
        except httpx.TimeoutException as exc:
            raise WeatherToolError("UPSTREAM_TIMEOUT", "Open-Meteo 请求超时") from exc
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise WeatherToolError("UPSTREAM_UNAVAILABLE", "Open-Meteo 暂时不可用") from exc

        try:
            current = forecast_payload["current"]
            code = int(current["weather_code"])
            return {
                "location": str(place.get("name") or normalized),
                "country": str(place.get("country") or ""),
                "timezone": str(forecast_payload.get("timezone") or place.get("timezone") or ""),
                "observedAt": str(current["time"]),
                "temperatureC": float(current["temperature_2m"]),
                "apparentTemperatureC": float(current["apparent_temperature"]),
                "relativeHumidityPercent": int(current["relative_humidity_2m"]),
                "precipitationMm": float(current["precipitation"]),
                "weatherCode": code,
                "weatherText": WMO_WEATHER_TEXT.get(code, "未知天气"),
                "windSpeedKmh": float(current["wind_speed_10m"]),
                "source": "Open-Meteo",
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherToolError("UPSTREAM_UNAVAILABLE", "Open-Meteo 返回结构无效") from exc


def _number(payload: Any, key: str) -> float:
    if not isinstance(payload, dict):
        raise ValueError("地理编码候选结构无效")
    return float(payload[key])
