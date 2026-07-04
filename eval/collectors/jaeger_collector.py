"""Collect traces from Jaeger."""

from __future__ import annotations

from eval.protocols import RunContext
from eval.trace import Trace, fetch_traces, filter_by_run


class JaegerCollector:
    """Trace collector for Jaeger OTLP ingestion."""

    def __init__(
        self,
        jaeger_url: str = "http://localhost:16686",
        otel_endpoint: str = "http://host.docker.internal:4318",
    ) -> None:
        self.jaeger_url = jaeger_url
        self.otel_endpoint = otel_endpoint

    def exporter_env(self, run_context: RunContext) -> dict[str, str]:
        return {
            "COPILOT_OTEL_EXPORTER_TYPE": "otlp-http",
            "OTEL_EXPORTER_OTLP_ENDPOINT": self.otel_endpoint,
        }

    def collect(self, run_context: RunContext) -> list[Trace]:
        traces = fetch_traces(self.jaeger_url, run_id=run_context.run_id)
        return filter_by_run(traces, run_context.run_id)
