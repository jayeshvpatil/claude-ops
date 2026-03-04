"""OTEL gRPC collector that receives spans from Claude Code and stores them."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent import futures
from typing import Optional

try:
    import grpc
    from opentelemetry.proto.collector.logs.v1 import (
        logs_service_pb2,
        logs_service_pb2_grpc,
    )
    from opentelemetry.proto.collector.metrics.v1 import (
        metrics_service_pb2,
        metrics_service_pb2_grpc,
    )
    from opentelemetry.proto.collector.trace.v1 import (
        trace_service_pb2,
        trace_service_pb2_grpc,
    )
    HAS_GRPC = True
except ImportError:
    HAS_GRPC = False

from .models import ApiRequest, Store


def _attr(attributes, key: str):
    """Extract a value from an OTLP attribute list by key."""
    for attr in attributes:
        if attr.key == key:
            v = attr.value
            if v.HasField("string_value"):
                return v.string_value
            if v.HasField("int_value"):
                return v.int_value
            if v.HasField("double_value"):
                return v.double_value
            if v.HasField("bool_value"):
                return v.bool_value
    return None


class _MetricsServicer(metrics_service_pb2_grpc.MetricsServiceServicer):
    def __init__(self, store: Store, session_id: str, project: str):
        self._store = store
        self._session_id = session_id
        self._project = project
        self._store.upsert_session(session_id, project, time.time())

    def Export(self, request, context):
        for rm in request.resource_metrics:
            sid = self._extract_session_id(rm) or self._session_id
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name == "claude_code.api_request":
                        self._handle_api_request(metric, sid)
        return metrics_service_pb2.ExportMetricsServiceResponse()

    def _extract_session_id(self, rm) -> Optional[str]:
        for attr in rm.resource.attributes:
            if attr.key == "session.id":
                return attr.value.string_value
        return None

    def _handle_api_request(self, metric, session_id: str):
        for dp in metric.gauge.data_points:
            attrs = dp.attributes
            req = ApiRequest(
                session_id=session_id,
                timestamp=dp.time_unix_nano / 1e9,
                model=_attr(attrs, "model") or "unknown",
                input_tokens=int(_attr(attrs, "input_tokens") or 0),
                output_tokens=int(_attr(attrs, "output_tokens") or 0),
                cache_read_tokens=int(_attr(attrs, "cache_read_tokens") or 0),
                cache_creation_tokens=int(
                    _attr(attrs, "cache_creation_tokens") or 0
                ),
                cost_usd=float(_attr(attrs, "cost_usd") or 0.0),
                duration_ms=int(_attr(attrs, "duration_ms") or 0),
                tool_name=_attr(attrs, "tool_name"),
                prompt_snippet=_attr(attrs, "prompt_snippet"),
            )
            self._store.insert_request(req)


class _LogsServicer(logs_service_pb2_grpc.LogsServiceServicer):
    """Accept logs (Claude Code also sends usage via logs)."""

    def __init__(self, store: Store, session_id: str):
        self._store = store
        self._session_id = session_id

    def Export(self, request, context):
        for rl in request.resource_logs:
            for sl in rl.scope_logs:
                for lr in sl.log_records:
                    self._handle_log(lr)
        return logs_service_pb2.ExportLogsServiceResponse()

    def _handle_log(self, lr):
        body = lr.body.string_value if lr.body.HasField("string_value") else ""
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return

        if data.get("event") != "claude_code.api_request":
            return

        sid = _attr(lr.attributes, "session.id") or self._session_id
        req = ApiRequest(
            session_id=sid,
            timestamp=lr.time_unix_nano / 1e9 if lr.time_unix_nano else time.time(),
            model=data.get("model", "unknown"),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cache_read_tokens=data.get("cache_read_tokens", 0),
            cache_creation_tokens=data.get("cache_creation_tokens", 0),
            cost_usd=data.get("cost_usd", 0.0),
            duration_ms=data.get("duration_ms", 0),
            tool_name=data.get("tool_name"),
            prompt_snippet=data.get("prompt_snippet"),
        )
        self._store.insert_request(req)


class _TraceServicer(trace_service_pb2_grpc.TraceServiceServicer):
    def Export(self, request, context):
        return trace_service_pb2.ExportTraceServiceResponse()


class Collector:
    """Lightweight OTEL gRPC collector for Claude Code telemetry."""

    def __init__(
        self,
        store: Store,
        session_id: str,
        project: str,
        port: int = 4317,
    ):
        if not HAS_GRPC:
            raise RuntimeError(
                "grpcio and opentelemetry-proto are required. "
                "Run: pip install grpcio opentelemetry-proto"
            )
        self._store = store
        self._session_id = session_id
        self._project = project
        self._port = port
        self._server: Optional[grpc.Server] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))

        metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(
            _MetricsServicer(self._store, self._session_id, self._project),
            self._server,
        )
        logs_service_pb2_grpc.add_LogsServiceServicer_to_server(
            _LogsServicer(self._store, self._session_id),
            self._server,
        )
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(
            _TraceServicer(), self._server
        )

        self._server.add_insecure_port(f"[::]:{self._port}")
        self._server.start()

    def stop(self):
        if self._server:
            self._store.end_session(self._session_id)
            self._server.stop(grace=2)

    def env_vars(self) -> dict[str, str]:
        """Return environment variables to set before launching Claude Code."""
        return {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://localhost:{self._port}",
        }
