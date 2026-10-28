"""Pure ASGI middleware: observe lifecycle without inspecting message bodies."""

import asyncio
import time
import uuid

from service.events import METHODS, ROUTES, emit


class RequestEvents:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        route = scope.get("path", "")
        method = scope.get("method", "")
        base = {
            "request_id": uuid.uuid4().hex,
            "route": route if route in ROUTES else "other",
            "method": method if method in METHODS else "other",
        }
        started = time.monotonic()
        status, outcome = 500, "error"
        emit({"event": "request_started", **base})

        async def observed_send(message):
            nonlocal status, outcome
            if message["type"] == "http.response.start":
                # Framework error handlers may supply an HTTPStatus IntEnum.
                status = int(message["status"])
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                outcome = "complete"

        async def observed_receive():
            nonlocal outcome
            message = await receive()
            if message["type"] == "http.disconnect" and outcome != "complete":
                outcome = "disconnect"
            return message

        try:
            await self.app(scope, observed_receive, observed_send)
        except asyncio.CancelledError:
            outcome = "disconnect"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            emit({"event": "request_finished", **base, "status": status,
                  "duration_ms": (time.monotonic() - started) * 1000, "outcome": outcome})
