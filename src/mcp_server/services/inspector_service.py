"""
MCP Inspector Service - External MCP server interaction tools.

Provides agents (e.g., TechnicalSupportAgent) with capabilities to:
- Connect to external MCP servers via streamable-http/SSE
- Discover tools, resources, and prompts on connected servers
- Execute tools on external servers
- Read resources from external servers
- List and manage connected server sessions

Architecture:
- Uses httpx for direct JSON-RPC 2.0 over streamable-http
- Maintains in-memory registry of connected server sessions
- Each connection performs full MCP handshake (initialize → initialized)
- Session-based: each connected server gets a unique session
"""

import asyncio
import json
import logging
import os
import uuid
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from core.factory import MCPToolBase, Domain
from utils.formatters import format_success_response, format_error_response

logger = logging.getLogger(__name__)


class ExternalMCPSession:
    """Manages a single connection to an external MCP server."""

    def __init__(self, server_url: str, server_name: str):
        self.server_url = server_url.rstrip("/")
        self.server_name = server_name
        self.client = httpx.AsyncClient(timeout=30.0)
        self.session_id: Optional[str] = None
        self._initialized: bool = False
        self.server_info: Dict[str, Any] = {}
        self.extra_headers: Dict[str, str] = {}
        self.connected_at: float = time.time()

    async def initialize(self) -> Dict[str, Any]:
        """Perform MCP handshake with the external server."""
        try:
            # Step 1: initialize
            init_result = await self._call_jsonrpc(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"roots": {"listChanged": False}},
                    "clientInfo": {
                        "name": "MACAE-Inspector-Bridge",
                        "version": "1.0.0",
                    },
                },
                skip_init_check=True,
            )

            self.server_info = init_result.get("serverInfo", {})

            # Step 2: Send initialized notification
            await self._send_notification("notifications/initialized")

            self._initialized = True
            logger.info(
                f"Connected to external MCP server: "
                f"{self.server_info.get('name', self.server_name)} "
                f"at {self.server_url}"
            )
            return self.server_info

        except Exception as e:
            logger.error(f"Failed to initialize connection to {self.server_url}: {e}")
            raise

    async def _send_notification(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> None:
        """Send JSON-RPC 2.0 notification (no id, fire-and-forget)."""
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        if self.extra_headers:
            headers.update(self.extra_headers)

        try:
            async with self.client.stream(
                "POST", self.server_url, json=payload, headers=headers
            ) as response:
                response.raise_for_status()
                if "mcp-session-id" in response.headers:
                    self.session_id = response.headers["mcp-session-id"]
        except Exception as e:
            logger.warning(f"Notification {method} failed: {e}")

    async def _call_jsonrpc(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        skip_init_check: bool = False,
    ) -> Dict[str, Any]:
        """Call JSON-RPC 2.0 method on the external server."""
        if not skip_init_check and not self._initialized:
            await self.initialize()

        request_id = str(uuid.uuid4())
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        if self.extra_headers:
            headers.update(self.extra_headers)

        try:
            async with self.client.stream(
                "POST", self.server_url, json=payload, headers=headers
            ) as response:
                response.raise_for_status()
                if "mcp-session-id" in response.headers:
                    new_sid = response.headers["mcp-session-id"]
                    if new_sid != self.session_id:
                        self.session_id = new_sid

                body = await response.aread()
                result = self._parse_sse_response(body.decode("utf-8"))

                if "error" in result:
                    error = result["error"]
                    raise Exception(
                        f"JSON-RPC error {error.get('code')}: {error.get('message')}"
                    )
                return result.get("result", {})

        except httpx.HTTPStatusError as e:
            logger.error(
                f"HTTP {e.response.status_code} from {self.server_url}/{method}"
            )
            raise
        except Exception as e:
            logger.error(f"Error calling {method} on {self.server_url}: {e}")
            raise

    def _parse_sse_response(self, body: str) -> Dict[str, Any]:
        """Parse JSON-RPC response from SSE body."""
        for event in body.split("\n\n"):
            data_lines: List[str] = []
            for line in event.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            if not data:
                continue
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict) and "jsonrpc" in parsed:
                    return parsed
            except json.JSONDecodeError:
                continue

        # Fallback: try parsing the whole body as JSON
        try:
            parsed = json.loads(body.strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        raise ValueError("No valid JSON-RPC payload found in response")

    async def list_tools(self) -> List[Dict[str, Any]]:
        """List tools available on the external server."""
        result = await self._call_jsonrpc("tools/list")
        return result.get("tools", [])

    async def call_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Call a tool on the external server."""
        result = await self._call_jsonrpc(
            "tools/call", {"name": tool_name, "arguments": arguments}
        )
        return result

    async def list_resources(self) -> List[Dict[str, Any]]:
        """List resources available on the external server."""
        result = await self._call_jsonrpc("resources/list")
        return result.get("resources", [])

    async def read_resource(self, uri: str) -> Dict[str, Any]:
        """Read a resource from the external server."""
        result = await self._call_jsonrpc("resources/read", {"uri": uri})
        return result

    async def list_prompts(self) -> List[Dict[str, Any]]:
        """List prompts available on the external server."""
        result = await self._call_jsonrpc("prompts/list")
        return result.get("prompts", [])

    async def close(self):
        """Close the connection."""
        await self.client.aclose()
        self._initialized = False


class ProxiedStdioSession:
    """Manages a connection to a stdio MCP server via the Inspector proxy.

    The MCP Inspector proxy can spawn stdio-based MCP servers (e.g.,
    @modelcontextprotocol/server-github) and bridge them over SSE.

    Protocol:
    1. GET /stdio?transportType=stdio&command=...&args=... → SSE stream
       First SSE event (type=endpoint) gives: /message?sessionId=<uuid>
    2. POST /message?sessionId=<uuid> → sends JSON-RPC requests (returns 202)
    3. Responses come back via the SSE stream as event=message data=<json>

    This class keeps the SSE stream alive in a background task and
    exposes the same interface as ExternalMCPSession.
    """

    def __init__(
        self,
        proxy_url: str,
        command: str,
        args: List[str],
        server_name: str,
        env: Optional[Dict[str, str]] = None,
        proxy_auth_token: Optional[str] = None,
    ):
        self.proxy_url = proxy_url.rstrip("/")
        self.command = command
        self.args = args
        self.server_name = server_name
        self.env = env or {}
        self.proxy_auth_token = proxy_auth_token
        self.server_url = f"stdio://{command}/{'+'.join(args)}"

        self.client = httpx.AsyncClient(timeout=60.0)
        self.session_id: Optional[str] = None
        self._initialized: bool = False
        self.server_info: Dict[str, Any] = {}
        self.extra_headers: Dict[str, str] = {}
        self.connected_at: float = time.time()

        # SSE reader state
        self._msg_endpoint: Optional[str] = None
        self._msg_url: Optional[str] = None
        self._sse_task: Optional[asyncio.Task] = None
        self._response_queues: Dict[Any, asyncio.Queue] = {}
        self._sse_connected = asyncio.Event()
        self._sse_dead = asyncio.Event()  # set when SSE stream closes unexpectedly
        self._sse_stream = None
        self._request_id_counter = 0
        self._closing = False  # True when close() is called intentionally
        self.capabilities: Dict[str, Any] = {}  # server capabilities from initialize

    @property
    def is_alive(self) -> bool:
        """True if SSE stream is still running and session is usable."""
        if not self._initialized:
            return False
        if self._sse_dead.is_set():
            return False
        if self._sse_task and self._sse_task.done():
            return False
        return True

    def _next_id(self) -> int:
        self._request_id_counter += 1
        return self._request_id_counter

    def _build_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.proxy_auth_token:
            headers["x-mcp-proxy-auth"] = f"Bearer {self.proxy_auth_token}"
        headers.update(self.extra_headers)
        return headers

    async def _start_sse_reader(self) -> None:
        """Open the SSE stream to the proxy's /stdio endpoint."""
        # Build query string
        args_encoded = quote(" ".join(self.args))
        sse_url = (
            f"{self.proxy_url}/stdio"
            f"?transportType=stdio"
            f"&command={quote(self.command)}"
            f"&args={args_encoded}"
        )

        # Add env vars as query params
        for k, v in self.env.items():
            resolved = os.environ.get(k, v) if v.startswith("${") else v
            sse_url += f"&env.{quote(k)}={quote(resolved)}"

        headers = self._build_headers()
        headers["Accept"] = "text/event-stream"

        logger.info(f"[ProxiedStdio] Opening SSE to {sse_url}")

        self._sse_stream = self.client.stream("GET", sse_url, headers=headers)
        response = await self._sse_stream.__aenter__()

        # Start background reader
        self._sse_task = asyncio.create_task(
            self._read_sse(response), name=f"sse-{self.server_name}"
        )

        # Wait for the endpoint event
        try:
            await asyncio.wait_for(self._sse_connected.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Timed out waiting for stdio server "
                f"'{self.server_name}' to start via proxy"
            )

    async def _read_sse(self, response) -> None:
        """Background task: read SSE events and dispatch responses."""
        event_type = None
        try:
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()

                if line.startswith("event:"):
                    event_type = line[6:].strip()
                    continue

                if not line.startswith("data:"):
                    continue

                data = line[5:].strip()
                if not data:
                    continue

                if event_type == "endpoint":
                    self._msg_endpoint = data
                    self._msg_url = f"{self.proxy_url}{data}"
                    self.session_id = data.split("sessionId=")[-1]
                    logger.info(f"[ProxiedStdio] Session: {self.session_id}")
                    self._sse_connected.set()
                    event_type = None
                    continue

                if event_type == "message":
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning(f"[ProxiedStdio] Unparseable: {data[:100]}")
                        event_type = None
                        continue

                    # Response to a request we sent
                    msg_id = parsed.get("id")
                    if msg_id is not None and msg_id in self._response_queues:
                        await self._response_queues[msg_id].put(parsed)
                    # Notifications — log and ignore
                    elif "method" in parsed and "id" not in parsed:
                        logger.debug(
                            f"[ProxiedStdio] Notification: {parsed.get('method')}"
                        )

                event_type = None

        except (httpx.RemoteProtocolError, httpx.ReadError, asyncio.CancelledError):
            return  # normal stream close or intentional cancel — don't mark dead
        except Exception as e:
            if not e or not str(e).strip():  # empty exception = stream closed normally
                return
            logger.error(f"[ProxiedStdio] SSE reader error: {e}")
            if not self._closing:
                self._sse_dead.set()
                self._initialized = False
                for q in list(self._response_queues.values()):
                    await q.put(
                        {"error": {"code": -1, "message": "SSE connection lost"}}
                    )
                logger.info(f"[ProxiedStdio] Session {self.server_name} marked dead")

    async def _send_request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        request_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Send a JSON-RPC request and wait for the response via SSE."""
        needs_reconnect = (
            not self._msg_url
            or self._sse_dead.is_set()
            or (self._sse_task and self._sse_task.done())
        )
        if needs_reconnect:
            logger.info(f"[ProxiedStdio] '{self.server_name}' reconnecting...")
            # Close stale stream and recreate client to avoid reuse of closed connections
            if self._sse_stream:
                try:
                    await self._sse_stream.__aexit__(None, None, None)
                except Exception:
                    pass
                self._sse_stream = None
            if not self.client.is_closed:
                await self.client.aclose()
            self.client = httpx.AsyncClient(timeout=60.0)
            self._sse_dead.clear()
            self._initialized = False
            self._sse_connected = asyncio.Event()
            self._msg_url = None
            self._response_queues.clear()
            await self.initialize()

        rid = request_id or self._next_id()
        q: asyncio.Queue = asyncio.Queue()
        self._response_queues[rid] = q

        payload = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params or {},
        }
        headers = self._build_headers()
        headers["Content-Type"] = "application/json"

        try:
            resp = await self.client.post(self._msg_url, json=payload, headers=headers)
            if resp.status_code not in (200, 202):
                raise RuntimeError(f"POST {method} returned {resp.status_code}")

            # Wait for response on SSE stream
            result = await asyncio.wait_for(q.get(), timeout=30.0)

            if "error" in result:
                error = result["error"]
                raise Exception(
                    f"JSON-RPC error {error.get('code')}: {error.get('message')}"
                )
            return result.get("result", {})

        finally:
            self._response_queues.pop(rid, None)

    async def _send_notification(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        if not self._msg_url:
            raise RuntimeError("Not connected")

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        headers = self._build_headers()
        headers["Content-Type"] = "application/json"

        await self.client.post(self._msg_url, json=payload, headers=headers)

    async def initialize(self) -> Dict[str, Any]:
        """Spawn the stdio server via proxy and complete MCP handshake."""
        try:
            # Start the SSE connection and wait for sessionId
            await self._start_sse_reader()

            # initialize
            init_result = await self._send_request(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"roots": {"listChanged": False}},
                    "clientInfo": {
                        "name": "MACAE-Inspector-Bridge",
                        "version": "1.0.0",
                    },
                },
            )

            self.server_info = init_result.get("serverInfo", {})
            self.capabilities = init_result.get("capabilities", {})

            # initialized notification
            await self._send_notification("notifications/initialized")

            self._initialized = True
            logger.info(
                f"[ProxiedStdio] Connected to "
                f"{self.server_info.get('name', self.server_name)} "
                f"via Inspector proxy (caps={list(self.capabilities.keys())})"
            )
            return self.server_info

        except Exception as e:
            logger.error(f"[ProxiedStdio] Failed to initialize {self.server_name}: {e}")
            raise

    async def list_tools(self) -> List[Dict[str, Any]]:
        result = await self._send_request("tools/list")
        return result.get("tools", [])

    async def call_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._send_request(
            "tools/call", {"name": tool_name, "arguments": arguments}
        )

    async def list_resources(self) -> List[Dict[str, Any]]:
        if "resources" not in self.capabilities:
            return []
        result = await self._send_request("resources/list")
        return result.get("resources", [])

    async def read_resource(self, uri: str) -> Dict[str, Any]:
        return await self._send_request("resources/read", {"uri": uri})

    async def list_prompts(self) -> List[Dict[str, Any]]:
        if "prompts" not in self.capabilities:
            return []
        result = await self._send_request("prompts/list")
        return result.get("prompts", [])

    async def close(self) -> None:
        """Shut down the SSE reader and HTTP client."""
        self._closing = True
        self._sse_dead.set()
        self._initialized = False
        if self._sse_task and not self._sse_task.done():
            self._sse_task.cancel()
            try:
                await self._sse_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._sse_stream:
            try:
                await self._sse_stream.__aexit__(None, None, None)
            except Exception:
                pass
        try:
            await self.client.aclose()
        except Exception:
            pass
        logger.info(f"[ProxiedStdio] Closed session '{self.server_name}'")


def _load_inspector_config() -> Dict[str, Any]:
    """Load the mcp-inspector-config.json file if it exists."""
    config_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "mcp-inspector-config.json"
    )
    config_path = os.path.normpath(config_path)
    try:
        with open(config_path) as f:
            return json.load(f)
    except Exception as e:
        logger.debug(f"Could not load inspector config: {e}")
        return {}


def _detect_inspector_token() -> str:
    """Auto-detect the Inspector proxy auth token from .inspector.log.

    The Inspector writes a line like:
        MCP_PROXY_AUTH_TOKEN=<hex>
    into .inspector.log each time it starts.  We read the last
    occurrence so the token stays current after restarts.
    """
    import re

    log_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", ".inspector.log"
    )
    log_path = os.path.normpath(log_path)
    try:
        with open(log_path) as f:
            content = f.read()
        matches = re.findall(r"MCP_PROXY_AUTH_TOKEN=([a-f0-9]{64})", content)
        if matches:
            token = matches[-1]
            logger.info(f"[Inspector] Auto-detected proxy token: {token[:12]}...")
            return token
    except Exception as e:
        logger.debug(f"Could not read inspector log for token: {e}")
    return ""


class RegistryBridge:
    """HTTP bridge to MCPConnectionsService via backend API.

    Allows the MCP server's InspectorService to look up servers
    registered in the Cosmos DB catalog, check user authorization,
    and initiate connections — all via REST calls to the backend.
    """

    def __init__(self):
        backend_url = os.environ.get(
            "MACAE_BACKEND_URL", "http://localhost:8000"
        ).rstrip("/")
        self.base = f"{backend_url}/api/v4/mcp/connections"
        self._client = httpx.AsyncClient(timeout=10.0)

    def _user_headers(self, user_id: str) -> Dict[str, str]:
        """Build auth headers for requests on behalf of a user."""
        return {
            "x-ms-client-principal-id": user_id,
            "x-ms-client-principal-name": user_id,
        }

    async def lookup_server(self, server_name: str) -> Optional[Dict[str, Any]]:
        """Look up a server in the catalog by name."""
        try:
            resp = await self._client.get(f"{self.base}/servers")
            resp.raise_for_status()
            for s in resp.json().get("servers", []):
                if s.get("server_name") == server_name:
                    return s
            return None
        except Exception as e:
            logger.warning(f"Registry lookup failed: {e}")
            return None

    async def list_catalog(self) -> List[Dict[str, Any]]:
        """List all enabled servers in the catalog."""
        try:
            resp = await self._client.get(f"{self.base}/servers")
            resp.raise_for_status()
            return resp.json().get("servers", [])
        except Exception as e:
            logger.warning(f"Registry catalog fetch failed: {e}")
            return []

    async def get_user_connection(
        self, user_id: str, server_name: str
    ) -> Optional[Dict[str, Any]]:
        """Get a user's connection status for a specific server."""
        try:
            resp = await self._client.get(
                f"{self.base}/user/{server_name}",
                headers=self._user_headers(user_id),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"User connection check failed: {e}")
            return None

    async def initiate_connection(
        self, user_id: str, server_name: str
    ) -> Optional[Dict[str, Any]]:
        """Initiate / create a user connection to a cataloged server."""
        try:
            resp = await self._client.post(
                f"{self.base}/user/{server_name}/connect",
                headers=self._user_headers(user_id),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"Connection initiation failed: {e}")
            return None

    async def activate_connection(
        self, user_id: str, server_name: str, secret_ref: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Mark a user connection as active (after OAuth callback)."""
        try:
            body = {}
            if secret_ref:
                body["secret_ref"] = secret_ref
            resp = await self._client.patch(
                f"{self.base}/user/{server_name}/activate",
                headers=self._user_headers(user_id),
                json=body,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"Connection activation failed: {e}")
            return None

    async def get_user_servers(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all servers with the user's connection status."""
        try:
            resp = await self._client.get(
                f"{self.base}/user",
                headers=self._user_headers(user_id),
            )
            resp.raise_for_status()
            return resp.json().get("connections", [])
        except Exception as e:
            logger.warning(f"User servers fetch failed: {e}")
            return []

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()


class InspectorService(MCPToolBase):
    """
    MCP Inspector tools for connecting to and interacting
    with external MCP servers.

    Enables TechnicalSupportAgent and other agents to:
    - Discover external MCP servers and their capabilities
    - Execute tools across server boundaries
    - Read resources from any connected MCP server
    - Look up servers from the Cosmos DB registry catalog
    - Check user authorization before connecting
    """

    def __init__(self):
        super().__init__(Domain.INSPECTOR)
        self._sessions: Dict[str, ExternalMCPSession] = {}
        self._proxied_sessions: Dict[str, ProxiedStdioSession] = {}
        self._registry = RegistryBridge()
        self._inspector_config = _load_inspector_config()
        self._proxy_url = os.environ.get(
            "MCP_INSPECTOR_PROXY_URL", "http://localhost:16277"
        )
        # Token is resolved dynamically — see _get_proxy_token()
        self._cached_proxy_token: str = ""

    def _get_proxy_token(self) -> str:
        """Return the current Inspector proxy auth token.

        Resolution order:
        1. MCP_INSPECTOR_AUTH_TOKEN env-var (explicit override)
        2. Auto-detect from .inspector.log (survives Inspector restarts)
        3. Cached value from last successful detection
        """
        env_token = os.environ.get("MCP_INSPECTOR_AUTH_TOKEN", "")
        if env_token:
            return env_token
        detected = _detect_inspector_token()
        if detected:
            self._cached_proxy_token = detected
            return detected
        return self._cached_proxy_token

    def register_tools(self, mcp) -> None:
        """Register inspector tools with the MCP server."""

        # Keep references in closure
        sessions = self._sessions
        proxied_sessions = self._proxied_sessions
        registry = self._registry
        inspector_config = self._inspector_config
        proxy_url = self._proxy_url
        get_proxy_token = self._get_proxy_token

        def _all_sessions() -> Dict[str, Any]:
            """Merge direct and proxied sessions for uniform lookup."""
            merged: Dict[str, Any] = {}
            merged.update(sessions)
            merged.update(proxied_sessions)
            return merged

        @mcp.tool(tags={self.domain.value})
        async def connect_mcp_server(
            server_url: str = "", server_name: str = ""
        ) -> str:
            """
            Connect to an external MCP server.

            Establishes a JSON-RPC session with the target server,
            performing the full MCP handshake (initialize + initialized).

            Supports two modes:
            - **Direct URL**: provide ``server_url`` (and optionally
              ``server_name``).
            - **Registry lookup**: provide ``server_name`` only; the
              endpoint URL is resolved from the Cosmos DB catalog.

            Args:
                server_url: Full URL of the MCP server endpoint
                           (e.g., "http://host:port/mcp").
                           Leave empty to look up by server_name.
                server_name: Friendly name for the server.
                             Auto-generated from URL if not provided.

            Returns:
                Connection status with server info and capabilities.
            """
            try:
                # --- Registry lookup when no URL provided ---
                if not server_url and server_name:
                    catalog_entry = await registry.lookup_server(server_name)
                    if not catalog_entry:
                        return format_error_response(
                            error_message=(
                                f"Server '{server_name}' not found "
                                f"in registry. Provide a server_url "
                                f"for direct connection, or register "
                                f"the server first."
                            ),
                            context="registry lookup",
                        )
                    server_url = catalog_entry.get("endpoint", "")
                    if not server_url:
                        return format_error_response(
                            error_message=(
                                f"Server '{server_name}' found in "
                                f"registry but has no endpoint."
                            ),
                            context="registry lookup",
                        )
                    logger.info(
                        f"Resolved '{server_name}' from registry -> {server_url}"
                    )

                if not server_url:
                    return format_error_response(
                        error_message=(
                            "Either server_url or server_name is "
                            "required. Provide a URL for direct "
                            "connection or a server_name for "
                            "registry lookup."
                        ),
                        context="connecting to MCP server",
                    )

                if not server_name:
                    from urllib.parse import urlparse

                    parsed = urlparse(server_url)
                    server_name = f"{parsed.hostname}:{parsed.port or 80}"

                # Check if already connected
                if server_name in sessions:
                    existing = sessions[server_name]
                    if existing._initialized:
                        return format_success_response(
                            action="Server Already Connected",
                            details={
                                "server_name": server_name,
                                "server_url": existing.server_url,
                                "server_info": existing.server_info,
                                "connected_at": existing.connected_at,
                            },
                            summary=(
                                f"Already connected to "
                                f"'{server_name}'. Use "
                                f"discover_mcp_capabilities "
                                f"to explore."
                            ),
                        )

                # Create and initialize new session
                session = ExternalMCPSession(server_url, server_name)
                server_info = await session.initialize()

                sessions[server_name] = session

                return format_success_response(
                    action="MCP Server Connected",
                    details={
                        "server_name": server_name,
                        "server_url": server_url,
                        "server_info": server_info,
                        "protocol_version": "2025-11-25",
                    },
                    summary=(
                        f"Successfully connected to MCP server "
                        f"'{server_info.get('name', server_name)}' "
                        f"at {server_url}. "
                        f"Use discover_mcp_capabilities("
                        f"'{server_name}') to see available tools "
                        f"and resources."
                    ),
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e),
                    context=(f"connecting to MCP server at {server_url}"),
                )

        @mcp.tool(tags={self.domain.value})
        async def discover_mcp_capabilities(server_name: str) -> str:
            """
            Discover all capabilities (tools, resources, prompts) on a
            connected external MCP server.

            Args:
                server_name: Name of a previously connected server.

            Returns:
                Complete capability listing including tools with
                descriptions, resources, and prompts.
            """
            try:
                all_sess = _all_sessions()
                if server_name not in all_sess:
                    available = list(all_sess.keys())
                    return format_error_response(
                        error_message=(
                            f"Server '{server_name}' not connected. "
                            f"Available servers: {available or 'none'}. "
                            f"Use connect_mcp_server first."
                        ),
                        context="discovering capabilities",
                    )

                session = all_sess[server_name]

                # Discover all capabilities in parallel
                tools = []
                resources = []
                prompts = []

                try:
                    tools = await session.list_tools()
                except Exception as e:
                    logger.warning(f"Failed to list tools from {server_name}: {e}")

                try:
                    resources = await session.list_resources()
                except Exception as e:
                    logger.warning(f"Failed to list resources from {server_name}: {e}")

                try:
                    prompts = await session.list_prompts()
                except Exception as e:
                    logger.warning(f"Failed to list prompts from {server_name}: {e}")

                capabilities = {
                    "server_name": server_name,
                    "server_url": session.server_url,
                    "server_info": session.server_info,
                    "tools": [
                        {
                            "name": t.get("name"),
                            "description": t.get("description", ""),
                            "inputSchema": t.get("inputSchema", {}),
                        }
                        for t in tools
                    ],
                    "resources": [
                        {
                            "uri": r.get("uri"),
                            "name": r.get("name", ""),
                            "description": r.get("description", ""),
                            "mimeType": r.get("mimeType", ""),
                        }
                        for r in resources
                    ],
                    "prompts": [
                        {
                            "name": p.get("name"),
                            "description": p.get("description", ""),
                        }
                        for p in prompts
                    ],
                    "summary": {
                        "total_tools": len(tools),
                        "total_resources": len(resources),
                        "total_prompts": len(prompts),
                    },
                }

                return format_success_response(
                    action="Capabilities Discovered",
                    details=capabilities,
                    summary=(
                        f"Server '{server_name}' has "
                        f"{len(tools)} tools, "
                        f"{len(resources)} resources, "
                        f"{len(prompts)} prompts. "
                        f"Use call_external_tool or read_external_resource "
                        f"to interact."
                    ),
                )

            except Exception as e:
                return format_error_response(
                    error_message=str(e),
                    context=f"discovering capabilities on '{server_name}'",
                )

        @mcp.tool(tags={self.domain.value})
        async def call_external_tool(
            server_name: str,
            target_tool: str,
            arguments: dict[str, Any] | None = None,
        ) -> str:
            """
            Execute a tool on a connected external MCP server.

            Args:
                server_name: Name of a previously connected server.
                target_tool: Name of the tool to execute on that server.
                arguments: Tool arguments as a JSON object
                          (e.g., {"query": "hello"}).

            Returns:
                Tool execution result from the external server.
            """
            try:
                all_sess = _all_sessions()
                if server_name not in all_sess:
                    available = list(all_sess.keys())
                    return format_error_response(
                        error_message=(
                            f"Server '{server_name}' not connected. "
                            f"Available: {available or 'none'}."
                        ),
                        context="calling external tool",
                    )

                session = all_sess[server_name]

                # Parse arguments — accept dict (normal), string (legacy), or None
                if isinstance(arguments, dict):
                    args = arguments
                elif isinstance(arguments, str):
                    try:
                        args = json.loads(arguments) if arguments else {}
                    except json.JSONDecodeError as e:
                        return format_error_response(
                            error_message=f"Invalid JSON arguments: {e}",
                            context="parsing tool arguments",
                        )
                else:
                    args = {}

                # Call the tool
                result = await session.call_tool(target_tool, args)

                # Extract content from MCP tool result
                content_parts = result.get("content", [])
                text_content = ""
                for part in content_parts:
                    if part.get("type") == "text":
                        text_content += part.get("text", "")

                return format_success_response(
                    action="TOOL SUCCESS - External Tool Executed",
                    details={
                        "server_name": server_name,
                        "target_tool": target_tool,
                        "arguments": args,
                        "result": text_content or result,
                        "is_error": False,  # Always False for successful tool calls
                        "status": "SUCCESS",
                    },
                    summary=(
                        f"✅ SUCCESS: Tool '{target_tool}' executed successfully on '{server_name}'. "
                        f"Result: {text_content[:200] if text_content else 'See details.'}"
                    ),
                )

            except Exception as e:
                return format_error_response(
                    error_message=str(e),
                    context=f"executing '{target_tool}' on '{server_name}'",
                )

        @mcp.tool(tags={self.domain.value})
        async def read_external_resource(server_name: str, resource_uri: str) -> str:
            """
            Read a resource from a connected external MCP server.

            Args:
                server_name: Name of a previously connected server.
                resource_uri: URI of the resource to read
                             (e.g., "file:///path" or "ui://widget").

            Returns:
                Resource content with MIME type.
            """
            try:
                all_sess = _all_sessions()
                if server_name not in all_sess:
                    available = list(all_sess.keys())
                    return format_error_response(
                        error_message=(
                            f"Server '{server_name}' not connected. "
                            f"Available: {available or 'none'}."
                        ),
                        context="reading external resource",
                    )

                session = all_sess[server_name]
                result = await session.read_resource(resource_uri)

                contents = result.get("contents", [])
                if not contents:
                    return format_error_response(
                        error_message=f"No content returned for {resource_uri}",
                        context="reading external resource",
                    )

                first = contents[0]
                content_text = first.get("text") or first.get("blob", "")
                mime_type = first.get("mimeType", "text/plain")

                return format_success_response(
                    action="External Resource Read",
                    details={
                        "server_name": server_name,
                        "resource_uri": resource_uri,
                        "mimeType": mime_type,
                        "content": (
                            content_text[:500]
                            if len(content_text) > 500
                            else content_text
                        ),
                        "content_length": len(content_text),
                    },
                    summary=(
                        f"Resource '{resource_uri}' from '{server_name}' "
                        f"({mime_type}, {len(content_text)} bytes)."
                    ),
                )

            except Exception as e:
                return format_error_response(
                    error_message=str(e),
                    context=f"reading '{resource_uri}' from '{server_name}'",
                )

        @mcp.tool(tags={self.domain.value})
        async def list_connected_servers() -> str:
            """
            List active sessions and registry catalog servers.

            Shows both in-memory connected sessions and servers
            registered in the Cosmos DB catalog (best-effort).

            Returns:
                Active sessions, registry catalog entries, and
                summary counts.
            """
            try:
                # --- Active RAM sessions (direct HTTP) ---
                active = []
                for name, session in sessions.items():
                    active.append(
                        {
                            "server_name": name,
                            "server_url": session.server_url,
                            "server_info": session.server_info,
                            "initialized": session._initialized,
                            "connected_at": session.connected_at,
                            "session_id": session.session_id,
                            "transport": "streamable-http",
                            "source": "ram_session",
                        }
                    )

                # --- Active proxied stdio sessions ---
                for name, psession in proxied_sessions.items():
                    active.append(
                        {
                            "server_name": name,
                            "server_url": psession.server_url,
                            "server_info": psession.server_info,
                            "initialized": psession._initialized,
                            "connected_at": psession.connected_at,
                            "session_id": psession.session_id,
                            "transport": "stdio-via-proxy",
                            "source": "proxied_session",
                        }
                    )

                # --- Inspector config servers (best-effort) ---
                inspector_servers = []
                cfg_servers = inspector_config.get("mcpServers", {})
                all_connected = set(sessions.keys()) | set(proxied_sessions.keys())
                for sname, sdef in cfg_servers.items():
                    transport = "streamable-http" if sdef.get("url") else "stdio"
                    inspector_servers.append(
                        {
                            "server_name": sname,
                            "transport": transport,
                            "is_connected": sname in all_connected,
                            "source": "inspector_config",
                        }
                    )

                # --- Registry catalog (best-effort) ---
                catalog = []
                try:
                    catalog_entries = await registry.list_catalog()
                    for entry in catalog_entries:
                        sname = entry.get("server_name", "")
                        catalog.append(
                            {
                                "server_name": sname,
                                "endpoint": entry.get("endpoint", ""),
                                "auth_type": entry.get("auth_type", "none"),
                                "display_name": entry.get("display_name", sname),
                                "description": entry.get("description", ""),
                                "is_connected": sname in all_connected,
                                "source": "registry",
                            }
                        )
                except Exception as e:
                    logger.debug(f"Registry catalog unavailable: {e}")

                total_active = len(active)
                total_catalog = len(catalog)
                total_inspector = len(inspector_servers)

                if not active and not catalog and not inspector_servers:
                    return format_success_response(
                        action="Connected Servers",
                        details={
                            "active_sessions": [],
                            "inspector_config_servers": [],
                            "registry_catalog": [],
                            "total_active": 0,
                            "total_inspector": 0,
                            "total_catalog": 0,
                        },
                        summary=(
                            "No active sessions, no inspector config, "
                            "and registry is empty or unavailable. "
                            "Use connect_mcp_server, "
                            "connect_stdio_server, or "
                            "connect_from_registry."
                        ),
                    )

                parts = []
                if total_active:
                    all_names = list(sessions.keys()) + list(proxied_sessions.keys())
                    parts.append(
                        f"{total_active} active session(s): {', '.join(all_names)}"
                    )
                not_connected = [
                    s["server_name"] for s in inspector_servers if not s["is_connected"]
                ]
                if not_connected:
                    parts.append(
                        f"{len(not_connected)} available in inspector "
                        f"config (not connected): "
                        f"{', '.join(not_connected)}. "
                        f"Use connect_stdio_server to connect."
                    )
                if total_catalog:
                    parts.append(f"{total_catalog} server(s) in registry")

                return format_success_response(
                    action="Connected Servers",
                    details={
                        "active_sessions": active,
                        "inspector_config_servers": inspector_servers,
                        "registry_catalog": catalog,
                        "total_active": total_active,
                        "total_inspector": total_inspector,
                        "total_catalog": total_catalog,
                    },
                    summary=". ".join(parts) + ".",
                )

            except Exception as e:
                return format_error_response(
                    error_message=str(e),
                    context="listing connected servers",
                )

        @mcp.tool(tags={self.domain.value})
        async def connect_from_registry(
            server_name: str, user_id: str = "sample_user"
        ) -> str:
            """
            Connect to an MCP server from the registry with
            user authorization check.

            Looks up the server in the Cosmos DB catalog, verifies
            user authorization, and connects if allowed.  For
            servers requiring authentication (OAuth2, API key, etc.)
            the tool initiates the auth flow and returns
            instructions.

            Args:
                server_name: Name of the server in the registry
                             (e.g., "youtube-api").
                user_id: User ID for authorization check.
                         Defaults to "sample_user" in dev.

            Returns:
                Connection result or auth instructions.
            """
            try:
                # 1. Lookup server in catalog
                server = await registry.lookup_server(server_name)
                if not server:
                    catalog = await registry.list_catalog()
                    available = [s.get("server_name") for s in catalog]
                    return format_error_response(
                        error_message=(
                            f"Server '{server_name}' not found in "
                            f"registry. Available: "
                            f"{available or 'none'}."
                        ),
                        context="registry lookup",
                    )

                endpoint = server.get("endpoint", "")
                auth_type = server.get("auth_type", "none")

                # 2. Initiate / check user connection
                conn_result = await registry.initiate_connection(user_id, server_name)
                if not conn_result:
                    return format_error_response(
                        error_message=(
                            f"Failed to initiate connection for "
                            f"user '{user_id}' to "
                            f"'{server_name}'. Backend API may "
                            f"be unavailable."
                        ),
                        context="connection initiation",
                    )

                connection = conn_result.get("connection", {})
                status = connection.get("status", "unknown")

                # 3. Handle pending auth
                if status == "pending_auth":
                    return format_success_response(
                        action="Authentication Required",
                        details={
                            "server_name": server_name,
                            "endpoint": endpoint,
                            "auth_type": auth_type,
                            "status": status,
                            "user_id": user_id,
                            "oauth_scopes": server.get("oauth_scopes", []),
                            "instructions": (
                                f"Server '{server_name}' requires "
                                f"{auth_type} authentication. "
                                f"Complete the authorization flow, "
                                f"then call connect_from_registry "
                                f"again."
                            ),
                        },
                        summary=(
                            f"Server '{server_name}' requires "
                            f"{auth_type} auth. User must "
                            f"authorize first, then retry."
                        ),
                    )

                # 4. Active — connect to endpoint
                if not endpoint:
                    return format_error_response(
                        error_message=(
                            f"Server '{server_name}' has no endpoint in the registry."
                        ),
                        context="endpoint resolution",
                    )

                # Build extra headers for auth servers
                extra_headers: Dict[str, str] = {}
                if auth_type != "none" and status == "active":
                    secret_ref = connection.get("secret_ref", "")
                    if secret_ref:
                        # TODO: #897 resolve from Key Vault
                        logger.info(
                            f"'{server_name}' secret_ref="
                            f"'{secret_ref}' — KV resolution "
                            f"not yet implemented"
                        )
                        extra_headers["X-MCP-Auth-Ref"] = secret_ref

                # Already connected?
                if server_name in sessions:
                    existing = sessions[server_name]
                    if existing._initialized:
                        return format_success_response(
                            action="Already Connected (Registry)",
                            details={
                                "server_name": server_name,
                                "server_url": existing.server_url,
                                "server_info": existing.server_info,
                                "auth_type": auth_type,
                                "user_id": user_id,
                                "registry_status": status,
                            },
                            summary=(
                                f"Already connected to "
                                f"'{server_name}' "
                                f"(auth={auth_type}, "
                                f"status={status})."
                            ),
                        )

                # Create session with optional auth headers
                session = ExternalMCPSession(endpoint, server_name)
                if extra_headers:
                    session.extra_headers = extra_headers

                server_info = await session.initialize()
                sessions[server_name] = session

                return format_success_response(
                    action="Connected from Registry",
                    details={
                        "server_name": server_name,
                        "endpoint": endpoint,
                        "server_info": server_info,
                        "auth_type": auth_type,
                        "user_id": user_id,
                        "registry_status": status,
                        "protocol_version": "2025-11-25",
                    },
                    summary=(
                        f"Connected to '{server_name}' via "
                        f"registry (auth={auth_type}, "
                        f"status={status}). Use "
                        f"discover_mcp_capabilities("
                        f"'{server_name}') to explore."
                    ),
                )

            except Exception as e:
                return format_error_response(
                    error_message=str(e),
                    context=(f"connecting to '{server_name}' from registry"),
                )

        @mcp.tool(tags={self.domain.value})
        async def disconnect_mcp_server(server_name: str) -> str:
            """
            Disconnect from an external MCP server.

            Args:
                server_name: Name of the server to disconnect from.

            Returns:
                Disconnection confirmation.
            """
            try:
                all_sess = _all_sessions()
                if server_name not in all_sess:
                    available = list(all_sess.keys())
                    return format_error_response(
                        error_message=(
                            f"Server '{server_name}' not connected. "
                            f"Available: {available or 'none'}."
                        ),
                        context="disconnecting server",
                    )

                # Remove from whichever dict it's in
                if server_name in sessions:
                    session = sessions.pop(server_name)
                else:
                    session = proxied_sessions.pop(server_name)
                await session.close()

                return format_success_response(
                    action="Server Disconnected",
                    details={
                        "server_name": server_name,
                        "server_url": session.server_url,
                    },
                    summary=f"Disconnected from '{server_name}'.",
                )

            except Exception as e:
                return format_error_response(
                    error_message=str(e),
                    context=f"disconnecting from '{server_name}'",
                )

        @mcp.tool(tags={self.domain.value})
        async def connect_stdio_server(server_name: str) -> str:
            """
            Connect to a stdio-based MCP server via the Inspector proxy.

            Looks up the server in mcp-inspector-config.json, spawns it
            through the Inspector proxy's stdio bridge, and performs the
            full MCP handshake.

            Use this for servers that run as local CLI processes
            (e.g., GitHub, filesystem, server-everything).

            Use ``list_connected_servers`` to see available server names
            from the inspector config.

            Args:
                server_name: Name of the server as defined in
                             mcp-inspector-config.json (e.g., "github",
                             "filesystem").

            Returns:
                Connection status with server info and tool count.
            """
            try:
                # Already connected?
                if server_name in proxied_sessions:
                    existing = proxied_sessions[server_name]
                    if existing.is_alive:
                        return format_success_response(
                            action="Already Connected (stdio)",
                            details={
                                "server_name": server_name,
                                "server_info": existing.server_info,
                                "transport": "stdio-via-proxy",
                            },
                            summary=(
                                f"Already connected to "
                                f"'{server_name}' via stdio proxy. "
                                f"Use discover_mcp_capabilities to "
                                f"explore."
                            ),
                        )
                    # Dead session — clean up and reconnect
                    logger.info(
                        f"[connect_stdio_server] Stale session "
                        f"'{server_name}' — reconnecting"
                    )
                    try:
                        await existing.close()
                    except Exception:
                        pass
                    del proxied_sessions[server_name]

                # Look up in inspector config
                cfg_servers = inspector_config.get("mcpServers", {})
                if server_name not in cfg_servers:
                    available = [
                        n
                        for n, d in cfg_servers.items()
                        if d.get("command")  # stdio servers only
                    ]
                    return format_error_response(
                        error_message=(
                            f"Server '{server_name}' not found in "
                            f"inspector config. Available stdio "
                            f"servers: {available or 'none'}."
                        ),
                        context="connecting stdio server",
                    )

                server_def = cfg_servers[server_name]
                command = server_def.get("command")
                if not command:
                    return format_error_response(
                        error_message=(
                            f"Server '{server_name}' is not a stdio "
                            f"server (no 'command' field). Use "
                            f"connect_mcp_server for URL-based servers."
                        ),
                        context="connecting stdio server",
                    )

                args = server_def.get("args", [])
                env = server_def.get("env", {})

                # Resolve env var placeholders — os.environ first, then Key Vault
                resolved_env = {}
                for k, v in env.items():
                    if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                        env_key = v[2:-1]
                        resolved = os.environ.get(env_key, "")

                        if not resolved:
                            # Attempt Key Vault resolution using secret name = env_key
                            # normalized to lowercase-hyphen (e.g. GITHUB_PERSONAL_ACCESS_TOKEN
                            # → github-personal-access-token)
                            try:
                                import sys as _sys

                                _backend = os.path.normpath(
                                    os.path.join(
                                        os.path.dirname(__file__), "..", "..", "backend"
                                    )
                                )
                                if _backend not in _sys.path:
                                    _sys.path.insert(0, _backend)
                                from credential_resolver import credential_resolver

                                secret_name = env_key.lower().replace("_", "-")
                                creds = await credential_resolver.resolve_by_secret_ref(
                                    secret_name
                                )
                                if creds:
                                    resolved = (
                                        creds.get("token")
                                        or creds.get("api_key")
                                        or creds.get("access_token")
                                        or creds.get(env_key)
                                        or next(iter(creds.values()), "")
                                    )
                                    if resolved:
                                        logger.info(
                                            f"[connect_stdio_server] Resolved '{env_key}' "
                                            f"for '{server_name}' from Key Vault "
                                            f"(secret='{secret_name}')"
                                        )
                            except Exception as kv_err:
                                logger.debug(
                                    f"[connect_stdio_server] KV lookup failed "
                                    f"for '{env_key}': {kv_err}"
                                )

                        if not resolved:
                            return format_error_response(
                                error_message=(
                                    f"Credential '{env_key}' required by "
                                    f"'{server_name}' not found in environment "
                                    f"or Key Vault (secret: "
                                    f"'{env_key.lower().replace('_', '-')}')."
                                ),
                                context="resolving server credentials",
                            )
                        resolved_env[k] = resolved
                    else:
                        resolved_env[k] = v

                # Create proxied session
                psession = ProxiedStdioSession(
                    proxy_url=proxy_url,
                    command=command,
                    args=args,
                    server_name=server_name,
                    env=resolved_env,
                    proxy_auth_token=get_proxy_token() or None,
                )

                server_info = await psession.initialize()
                proxied_sessions[server_name] = psession

                # Get tool count for summary
                try:
                    tools = await psession.list_tools()
                    tool_count = len(tools)
                except Exception:
                    tool_count = -1

                return format_success_response(
                    action="Stdio Server Connected",
                    details={
                        "server_name": server_name,
                        "server_info": server_info,
                        "command": command,
                        "args": args,
                        "transport": "stdio-via-proxy",
                        "tool_count": tool_count,
                    },
                    summary=(
                        f"Connected to stdio server "
                        f"'{server_info.get('name', server_name)}' "
                        f"via Inspector proxy "
                        f"({tool_count} tools). "
                        f"Use discover_mcp_capabilities('"
                        f"{server_name}') to explore."
                    ),
                )

            except Exception as e:
                return format_error_response(
                    error_message=str(e),
                    context=(
                        f"connecting to stdio server '{server_name}' "
                        f"via Inspector proxy"
                    ),
                )

    @property
    def tool_count(self) -> int:
        """Return the number of tools provided by this service."""
        return 8  # connect, discover, call, read, list, connect_from_registry, disconnect, connect_stdio
