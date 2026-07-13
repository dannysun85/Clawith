"""FastAPI middleware for request tracing and logging."""

import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.logging_config import set_trace_id
from loguru import logger


class TraceIdMiddleware(BaseHTTPMiddleware):
    """Middleware to inject trace ID into request context and log requests."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Never echo a client-supplied correlation value into operational logs.
        trace_id = set_trace_id(uuid.uuid4().hex[:12])

        # Add trace ID to request state for access in endpoints
        request.state.trace_id = trace_id

        start_time = time.time()

        # The raw path can contain OAuth codes, webhook tokens, or tenant data.
        logger.info(f"--> {request.method} request")

        try:
            response = await call_next(request)
            duration = time.time() - start_time

            # Add trace ID to response headers
            response.headers["X-Trace-Id"] = trace_id

            route_obj = request.scope.get("route")
            route_template = getattr(route_obj, "path", None) or "<unresolved>"

            # Log only the FastAPI route template, never the raw request path.
            logger.info(
                f"<-- {request.method} {route_template} "
                f"{response.status_code} {duration:.3f}s"
            )

            if response.status_code >= 500:
                from app.services.production_issue_monitor import record_production_issue

                await record_production_issue(
                    source="http_server",
                    category="api",
                    summary="Server returned an unsuccessful product operation response",
                    severity="error",
                    error_code=f"http_{response.status_code}",
                    route=route_template,
                    operation=request.method,
                    trace_id=trace_id,
                    metadata={
                        "status_code": response.status_code,
                        "http_method": request.method,
                        "duration_ms": round(duration * 1000),
                    },
                )

            return response

        except Exception as exc:
            duration = time.time() - start_time
            route_obj = request.scope.get("route")
            route_template = getattr(route_obj, "path", None) or "<unresolved>"
            logger.error(
                f"<-- {request.method} {route_template} "
                f"ERROR {duration:.3f}s error_type={type(exc).__name__}"
            )
            from app.services.production_issue_monitor import record_production_issue

            await record_production_issue(
                source="http_server",
                category="api",
                summary="Unhandled server exception during a product operation",
                severity="critical",
                error_code=type(exc).__name__,
                route=route_template,
                operation=request.method,
                trace_id=trace_id,
                metadata={
                    "error_type": type(exc).__name__,
                    "http_method": request.method,
                    "duration_ms": round(duration * 1000),
                },
            )
            raise
