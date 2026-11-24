"""The tau2 tools as an MCP server, in-process, over Streamable HTTP on localhost.

The relay executes nothing. When Claude Code calls a tau2 tool, the call is handed to the
participant waiting in tau2's Orchestrator, which returns it as an AssistantMessage (or, for
telecom's device tools, a UserMessage) with that one tool call. The Orchestrator executes it
on the task's Environment exactly as it does for tau2's own agents (sync_tools, error
counting, trajectory), and the ToolMessage it produces is what the relay answers. So the
trajectory, the final DB state and therefore tau2's grading are tau2's own.

One server serves every concurrent episode; each participant gets a route
/mcp/<key> with its own tool list and queue."""

import asyncio
import concurrent.futures
import contextlib
import queue
import re
import socket
import threading
import time

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount

_KEY_RE = re.compile(r"/mcp/([A-Za-z0-9_-]+)")


class Route:
    def __init__(self, tools: list, events: queue.Queue):
        self.events = events
        self.mcp_tools = []
        for t in tools:
            fn = t.openai_schema["function"]
            self.mcp_tools.append(types.Tool(name=fn["name"], description=fn.get("description") or "",
                                             inputSchema=fn["parameters"]))
        self.closed = False

    def submit(self, name: str, arguments: dict) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        if self.closed:
            fut.set_result(("Error: the conversation has ended.", True))
        else:
            self.events.put(("call", (name, arguments, fut)))
        return fut


class Relay:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, call_timeout: float = 1800.0):
        self.host = host
        self.port = port or _free_port()
        self.call_timeout = call_timeout
        self._routes: dict[str, Route] = {}
        self._lock = threading.Lock()
        server = Server("tau2")

        def route() -> Route:
            req = server.request_context.request
            m = _KEY_RE.search(req.url.path if req is not None else "")
            r = self._routes.get(m.group(1)) if m else None
            if r is None:
                raise ValueError("unknown or finished episode")
            return r

        @server.list_tools()
        async def _list_tools() -> list[types.Tool]:
            return route().mcp_tools

        @server.call_tool(validate_input=False)
        async def _call_tool(name: str, arguments: dict | None):
            fut = route().submit(name, dict(arguments or {}))
            text, is_error = await asyncio.wait_for(asyncio.wrap_future(fut), self.call_timeout)
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                        isError=bool(is_error))

        self._sm = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)

        async def handle(scope, receive, send):
            await self._sm.handle_request(scope, receive, send)

        @contextlib.asynccontextmanager
        async def lifespan(_app):
            async with self._sm.run():
                yield

        app = Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)
        self._server = uvicorn.Server(uvicorn.Config(app, host=self.host, port=self.port,
                                                     log_level="warning", lifespan="on"))
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="tau2-relay")
        self._thread.start()
        deadline = time.monotonic() + 30
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("tau2 relay did not start")
            time.sleep(0.05)

    def register(self, key: str, tools: list, events: queue.Queue) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError(f"bad relay key {key!r}")
        with self._lock:
            self._routes[key] = Route(tools, events)
        return f"http://{self.host}:{self.port}/mcp/{key}"

    def unregister(self, key: str) -> None:
        with self._lock:
            r = self._routes.pop(key, None)
        if r is not None:
            r.closed = True

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_relay: Relay | None = None
_relay_lock = threading.Lock()


def get_relay() -> Relay:
    global _relay
    with _relay_lock:
        if _relay is None:
            _relay = Relay()
        return _relay
