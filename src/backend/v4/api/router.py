import asyncio
import inspect
import json
import logging
import os
import re
import uuid
from contextlib import AsyncExitStack
from typing import Annotated, Any, Optional, cast

from agent_framework import AgentResponse, AgentResponseUpdate, Content, WorkflowEvent
from azure.core.exceptions import ResourceNotFoundError
from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from opentelemetry import trace
from pydantic import BaseModel

import v4.models.messages as messages
from auth.auth_utils import get_authenticated_user_details
from common.database.database_base import DatabaseBase
from common.database.database_factory import DatabaseFactory
from common.models.messages_af import (
    ChatMessageRequest,
    ChatMessageResponse,
    InitTeamQuery,
    InputTask,
    Plan,
    PlanStatus,
    ResumePlanRequest,
    TeamAgent,
    TeamConfiguration,
    TeamSelectionRequest,
)
from common.services.chat_cosmos_service import get_chat_cosmos_service
from common.services.event_store import EVENT_KINDS, get_event_store
from common.utils.event_utils import track_event_if_configured
from common.utils.utils_af import (
    find_first_available_team,
    rai_success,
    rai_validate_team_config,
)
from v4.common.models.mcp_connection_models import (
    McpReadResourceRequest,
    MCPServerEntry,
    MCPServerUpdateRequest,
    OAuthCallbackQuery,
)
from v4.common.services.plan_service import PlanService
from v4.common.services.team_service import TeamService
from v4.common.tool_errors import (
    ToolError,
    ToolErrorCategory,
    classify_tool_error,
    emit_tool_error,
    run_with_backoff,
    user_message_for,
)
from v4.config.settings import (
    connection_config,
    orchestration_config,
    team_config,
)
from v4.control.reconciler import get_reconciler
from v4.models.messages import WebsocketMessageType
from v4.models.models import MPlan
from v4.orchestration.orchestration_manager import OrchestrationManager

from .tool_activity import describe_tool_call

router = APIRouter()
logger = logging.getLogger(__name__)

_TURN_LOG_MARKER = "[turn-log]"
_TURN_LOG_BLOCK_RE = re.compile(r"(?is)\n*\[turn-log\][\s\S]*$")

# Turnos de chat en vuelo, por identidad (user_id, turn_id) → ¿abortado?
# El ingress de Container Apps NO propaga el cierre del cliente al contenedor
# (medido 2026-09-14 contra rev 118: socket cortado a 0 ms de las cabeceras y
# el turno igual se generó y persistió; en uvicorn directo sí cancela). Por eso
# la cancelación no puede depender del transporte: el cliente que aborta lo
# declara por identidad (POST /chat/turns/{turn_id}/abort) y el generador la
# consulta antes de seguir generando y antes de persistir.
_ACTIVE_TURNS: dict[tuple[str, str], bool] = {}


def _strip_turn_log_block(text: Any) -> str:
    """Remove persisted turn-log trailer from assistant text content."""
    value = str(text or "")
    if not value:
        return ""
    return _TURN_LOG_BLOCK_RE.sub("", value).strip()


# ── Deeds de herramientas: registro estructurado del turno ─────────────────
# Un deed es un registro con campos (server, tool, status, args, result), no
# un string. Nada se recorta en silencio: los campos largos llevan su longitud
# real y un flag explícito si se acotaron. Las cotas existen porque el doc de
# sesión en Cosmos acumula TODOS los turnos (límite 2 MB por ítem).
_LEDGER_MAX_DEEDS = 16
_DEED_ARGS_CAP = 4000
_DEED_RESULT_CAP = 20000
# Presupuesto de contexto al REPLAY (recover): el registro completo queda en
# metadata; al modelo se le muestra la cabeza y cuántos caracteres quedan.
_DEED_REPLAY_ARGS_CHARS = 600
_DEED_REPLAY_RESULT_CHARS = 1500

_TOOL_DEEDS_HEADER = (
    "Registro del backend (evidencia de ejecución, no texto del asistente): "
    "en el turno anterior del asistente el sistema ejecutó estas herramientas "
    "reales y obtuvo estos resultados. Úsalo como hecho verificado; no lo "
    "reproduzcas literalmente ni lo presentes como un registro propio."
)


def _bounded_field(text: Any, cap: int) -> dict:
    value = str(text or "")
    return {"text": value[:cap], "chars": len(value), "truncated": len(value) > cap}


def _make_deed(server: str, tool: str, args: Any, status: str, result: Any) -> dict:
    """Registro estructurado de UNA ejecución de herramienta (metadata.turn_log)."""
    return {
        "server": server,
        "tool": tool,
        "status": status,
        "args": _bounded_field(args, _DEED_ARGS_CAP),
        "result": _bounded_field(result, _DEED_RESULT_CAP),
    }


def _replay_head(field: Any, cap: int) -> str:
    """Cabeza de un campo para el contexto del modelo, con el resto declarado."""
    if isinstance(field, dict):
        text = str(field.get("text") or "")
        chars = int(field.get("chars") or len(text))
    else:
        text = str(field or "")
        chars = len(text)
    shown = text[:cap]
    rest = chars - len(shown)
    return shown + (f" (… {rest} caracteres más en el registro)" if rest > 0 else "")


def _render_deed(deed: Any) -> str:
    if isinstance(deed, dict):
        return (
            f"{deed.get('tool') or '?'} @ {deed.get('server') or '?'}"
            f" — estado: {deed.get('status') or '?'}"
            f" — args: {_replay_head(deed.get('args'), _DEED_REPLAY_ARGS_CHARS)}"
            f" — resultado: {_replay_head(deed.get('result'), _DEED_REPLAY_RESULT_CHARS)}"
        )
    # Legado: sesiones persistidas antes del registro estructurado guardaban
    # "server.tool(args) -> resultado" ya acotado; se muestra tal cual.
    return str(deed or "").strip()


def _tool_deeds_note(deeds: Any, dropped: Any = 0) -> str:
    """Render persisted ``metadata.turn_log`` as SYSTEM-attributed evidence.

    Los deeds salieron del ``content`` para que el modelo no los imite como
    prosa propia. Pero sin ellos el modelo tampoco puede saber que una
    respuesta previa fue producto de una tool real, y se "retracta" de
    resultados correctos (observado en vivo: devolvió el SHA real y añadió
    "ese SHA fue inventado por mí"). Se reinyectan con rol ``system``: el
    modelo no puede autorar ese rol, así que la evidencia nunca se confunde
    con su propia voz. Sin el marcador ``[turn-log]``: ese marcador es
    exclusivo del backend y su aparición en salida del modelo es fabricación
    (compuerta en ``_RouterChatClient``). Nunca se persiste ni se indexa: se
    construye en cada recover a partir de metadata.
    """
    if not isinstance(deeds, list):
        return ""
    lines = [line for line in (_render_deed(d) for d in deeds) if line]
    if not lines:
        return ""
    body = "\n".join(f"- {line}" for line in lines)
    try:
        extra = int(dropped or 0)
    except (TypeError, ValueError):
        extra = 0
    if extra > 0:
        body += f"\n- (+{extra} ejecuciones más de este turno sin registro)"
    return _TOOL_DEEDS_HEADER + "\n" + body


def _extract_auth(request: Request) -> tuple:
    """Extract (user_id, tenant_id) from request headers.

    Single point of auth extraction for all endpoints.
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    tenant_id = authenticated_user.get("tenant_id", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="no user found")
    return user_id, tenant_id


def _extract_auth_with_token(request: Request) -> tuple:
    """Extract (user_id, tenant_id, access_token) from request headers.

    Use this for endpoints that need the user's access token for OBO flow.
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    tenant_id = authenticated_user.get("tenant_id", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="no user found")
    return user_id, tenant_id, authenticated_user.get("access_token")


app_v4 = APIRouter(
    prefix="/api/v4",
    responses={
        # FastAPI answers 400 "There was an error parsing the body" on
        # malformed JSON for EVERY body-taking route — framework behavior,
        # so the contract must declare it (schemathesis: UndefinedStatusCode).
        400: {"description": "Malformed request body"},
        404: {"description": "Not found"},
    },
)

# Workspace endpoints: Monaco (browser) and MCP filesystem agents share one physical
# path — {MACAE_WORKSPACE_ROOT}/{user_id}/{workspace_id}/ — so there is a single
# source of truth per workspace regardless of which writer touches it.
from v4.api.audio_router import audio_router  # noqa: E402
from v4.api.workspace_router import workspace_router, workspaces_router  # noqa: E402

app_v4.include_router(workspaces_router)
app_v4.include_router(workspace_router)
app_v4.include_router(audio_router)


@app_v4.websocket("/socket/{process_id}")
async def start_comms(
    websocket: WebSocket, process_id: str, user_id: str = Query(None)
):
    """Web-Socket endpoint for real-time process status updates."""

    # Always accept the WebSocket connection first
    await websocket.accept()

    user_id = user_id or "00000000-0000-0000-0000-000000000000"

    # Manually create a span for WebSocket since excluded_urls suppresses auto-instrumentation.
    # Without this, all track_event_if_configured calls inside WebSocket would get operation_Id = 0.
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(
        "WebSocket_Connection",
        attributes={"process_id": process_id, "user_id": user_id},
    ) as ws_span:
        # Resolve session_id from plan for telemetry
        session_id = None
        try:
            memory_store = await DatabaseFactory.get_database(
                user_id=user_id, tenant_id=""
            )
            plan = await memory_store.get_plan_by_plan_id(plan_id=process_id)
            if plan:
                session_id = getattr(plan, "session_id", None)
                if session_id:
                    ws_span.set_attribute("session_id", session_id)
        except Exception as e:
            logging.warning(f"[websocket] Failed to resolve session_id: {e}")

        # Add to the connection manager for backend updates
        connection_config.add_connection(
            process_id=process_id, connection=websocket, user_id=user_id
        )
        ws_props = {"process_id": process_id, "user_id": user_id}
        if session_id:
            ws_props["session_id"] = session_id
        track_event_if_configured("WebSocket_Connected", ws_props)

        # Re-send whatever the parked plan is waiting for and this socket never
        # saw: a plan review (race: backend parks before the frontend connects)
        # or a clarification (a refresh while the plan waits on a question opens
        # a new socket; without the USER_CLARIFICATION_REQUEST the input stays
        # locked and the question can never be answered — measured 2026-09-21,
        # plan e5b31dda). Durable source: the plan's waiting_for, never memory.
        try:
            _ws_store = await DatabaseFactory.get_database(user_id=user_id)
            _parked = await _plan_waiting_for(
                _ws_store, kind="plan_review", plan_id=process_id
            )
            if _parked is not None and (_parked.waiting_for or {}).get("m_plan"):
                _wf = _parked.waiting_for or {}
                await connection_config.send_status_update_async(
                    message=messages.PlanApprovalRequest(
                        plan=MPlan.model_validate(_wf["m_plan"]),
                        status=messages.PlanStatus.PENDING_APPROVAL,
                        context={
                            "request_id": _wf.get("request_id"),
                            "is_stalled": bool(_wf.get("is_stalled")),
                        },
                    ),
                    user_id=user_id,
                    message_type=messages.WebsocketMessageType.PLAN_APPROVAL_REQUEST,
                    process_id=process_id,
                )
                logging.info(
                    "Re-sent pending PLAN_APPROVAL_REQUEST for plan %s to user %s",
                    _parked.plan_id,
                    user_id,
                )
            _asked = await _plan_waiting_for(
                _ws_store, kind="clarification", plan_id=process_id
            )
            if _asked is not None:
                _wfq = _asked.waiting_for or {}
                await connection_config.send_status_update_async(
                    {
                        "question": _wfq.get("question") or "",
                        "request_id": _wfq.get("request_id"),
                    },
                    user_id,
                    message_type=messages.WebsocketMessageType.USER_CLARIFICATION_REQUEST,
                    process_id=process_id,
                )
                logging.info(
                    "Re-sent pending USER_CLARIFICATION_REQUEST for plan %s to user %s",
                    _asked.plan_id,
                    user_id,
                )
        except Exception as e:
            logging.warning("Failed to re-send pending request on WS connect: %s", e)

        # Keep the connection open - FastAPI will close the connection if this returns
        try:
            # Keep the connection open - FastAPI will close the connection if this returns
            while True:
                # no expectation that we will receive anything from the client but this keeps
                # the connection open and does not take cpu cycle
                try:
                    message = await websocket.receive_text()
                    logging.debug(
                        f"Received WebSocket message from {user_id}: {message}"
                    )
                except asyncio.TimeoutError:
                    # Ignore timeouts to keep the WebSocket connection open, but avoid a tight loop.
                    logging.debug(
                        f"WebSocket receive timeout for user {user_id}, process {process_id}"
                    )
                    await asyncio.sleep(0.1)
                except WebSocketDisconnect:
                    dc_props = {"process_id": process_id, "user_id": user_id}
                    if session_id:
                        dc_props["session_id"] = session_id
                    track_event_if_configured("WebSocket_Disconnected", dc_props)
                    logging.info(f"Client disconnected from batch {process_id}")
                    break
        except Exception as e:
            # Fixed logging syntax - removed the error= parameter
            logging.error(f"Error in WebSocket connection: {str(e)}")
        finally:
            # Always clean up the connection
            await connection_config.close_connection(process_id=process_id)


@app_v4.get("/init_team")
async def init_team(
    request: Request,
    query: Annotated[InitTeamQuery, Query()],
):
    """Initialize the user's current team of agents"""
    team_switched = query.team_switched

    # Get first available team from 4 to 1 (RFP -> Retail -> Marketing -> HR)
    # Falls back to HR if no teams are available.
    print(f"Init team called, team_switched={team_switched}")
    try:
        user_id, tenant_id, user_access_token = _extract_auth_with_token(request)

        # Initialize memory store and service
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        team_service = TeamService(memory_store)

        init_team_id = await find_first_available_team(team_service, user_id)

        # Get current team if user has one
        user_current_team = await memory_store.get_current_team(user_id=user_id)

        # If no teams available and no current team, return empty state to allow custom team upload
        if not init_team_id and not user_current_team:
            print("No teams found in database. System ready for custom team upload.")
            return {
                "status": "No teams configured. Please upload a team configuration to get started.",
                "team_id": None,
                "team": None,
                "requires_team_upload": True,
            }

        # Use current team if available, otherwise use found team
        if user_current_team:
            init_team_id = user_current_team.team_id
            print(f"Using user's current team: {init_team_id}")
        elif init_team_id:
            print(f"Using first available team: {init_team_id}")
            user_current_team = await team_service.handle_team_selection(
                user_id=user_id, team_id=init_team_id
            )
            if user_current_team:
                init_team_id = user_current_team.team_id

        # Verify the team exists and user has access to it
        if not init_team_id:
            return {
                "status": "No team selected. Please select or upload a team configuration.",
                "team_id": None,
                "team": None,
                "requires_team_upload": True,
            }
        team_configuration = await team_service.get_team_configuration(
            init_team_id, user_id
        )
        if team_configuration is None:
            # If team doesn't exist, clear current team and return empty state
            await memory_store.delete_current_team(user_id)
            print(
                f"Team configuration '{init_team_id}' not found. Cleared current team."
            )
            return {
                "status": "Current team configuration not found. Please select or upload a team configuration.",
                "team_id": None,
                "team": None,
                "requires_team_upload": True,
            }

        # Set as current team in memory
        team_config.set_current_team(
            user_id=user_id, team_configuration=team_configuration
        )

        # Initialize agent team for this user session
        await OrchestrationManager.get_current_or_new_orchestration(
            user_id=user_id,
            team_config=team_configuration,
            team_switched=team_switched,
            team_service=team_service,
            user_access_token=user_access_token,  # OBO: run agents as the user
        )

        return {
            "status": "Request started successfully",
            "team_id": init_team_id,
            "team": team_configuration,
        }

    except Exception as e:
        track_event_if_configured(
            "Error_Init_Team_Failed",
            {
                "error": str(e),
            },
        )
        raise HTTPException(
            status_code=400, detail=f"Error starting request: {e}"
        ) from e


@app_v4.post("/process_request")
async def process_request(
    background_tasks: BackgroundTasks, input_task: InputTask, request: Request
):
    """
    Create a new plan without full processing.

    ---
    tags:
      - Plans
    parameters:
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            session_id:
              type: string
              description: Session ID for the plan
            description:
              type: string
              description: The task description to validate and create plan for
    responses:
      200:
        description: Plan created successfully
        schema:
          type: object
          properties:
            plan_id:
              type: string
              description: The ID of the newly created plan
            status:
              type: string
              description: Success message
            session_id:
              type: string
              description: Session ID associated with the plan
      400:
        description: RAI check failed or invalid input
        schema:
          type: object
          properties:
            detail:
              type: string
              description: Error message
    """
    user_id, tenant_id, user_access_token = _extract_auth_with_token(request)

    if not input_task.session_id:
        input_task.session_id = str(uuid.uuid4())

    # Attach session_id to current span for Application Insights
    span = trace.get_current_span()
    if span:
        span.set_attribute("session_id", input_task.session_id)

    # Plan position of the chat|plan selector: the roster is composed by the
    # Router from THIS request, exactly like the chat lane's escalation. The
    # selected-team default that used to live here handed a repository audit
    # to 'Product Marketing Team' (measured in Chrome, 2026-09-22).
    composer = _RouterChatClient(
        "Hosted Orchestrator",
        user_access_token=user_access_token,
        user_id=user_id,
        workspace_id=input_task.workspace_id,
    )
    try:
        pattern, task, roster = await composer.compose_plan(input_task.description)
    finally:
        await composer.close()
    logger.info("Plan position: composer pattern=%s", pattern)
    plan_id = await _create_plan_and_start(
        background_tasks=background_tasks,
        user_id=user_id,
        tenant_id=tenant_id,
        user_access_token=user_access_token,
        description=task,
        session_id=input_task.session_id,
        persist_user_task=True,
        composed_agents=roster,
        workspace_id=input_task.workspace_id,
    )
    return {
        "status": "Request started successfully",
        "session_id": input_task.session_id,
        "plan_id": plan_id,
    }


# Hard ceiling on a Router-composed roster. Magentic broadcasts every turn to
# every participant, so cost and round count grow with the roster; a task that
# genuinely needs more is a task that needs splitting.
_COMPOSED_TEAM_MAX_AGENTS = 4


async def _team_from_router_roster(
    roster: list,
    description: str,
    user_id: str,
    memory_store: Any,
    workspace_id: Optional[str] = None,
    with_proxy: bool = True,
    persist: bool = True,
) -> TeamConfiguration:
    """Turn the Model Router's ``run_plan`` roster into a persisted team.

    Reuses the SAME validator and persistence as the upload path
    (``TeamService.validate_and_parse_team_config`` / ``save_team_configuration``)
    — the roster is just another team-config source. Persisted because
    ``plan.team_id`` is resolved later by the in-plan chat lane and
    ``resume_plan``; an unpersisted team would 404 there.

    Factory constraints are re-checked HERE, in code (the prompt orients the
    Router; it guarantees nothing): supported deployment, reasoning XOR coding
    tools, no RAG (no index to point at), workspace access when the request
    carries one, reserved/duplicate names dropped, ProxyAgent appended (the human-in-the-loop clarification channel the
    factory special-cases by name). The team NAME is fixed: it becomes the
    Magentic manager's Foundry agent name (``sanitize(team.name)`` in
    ``init_orchestration``), and a per-task name would publish a new manager
    definition per request — version sprawl.
    """
    from common.config.app_config import config

    try:
        supported = json.loads(config.SUPPORTED_MODELS)
    except (TypeError, ValueError):
        supported = []
    deployment = config.AZURE_OPENAI_DEPLOYMENT_NAME
    if supported and deployment not in supported:
        deployment = str(supported[0])

    agents: list[dict] = []
    seen: set[str] = set()
    for raw in roster:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        # isidentifier(): no spaces/punctuation — the name becomes a Foundry
        # agent name and the Magentic participant key.
        if not name or not name.isidentifier():
            continue
        lowered = name.lower()
        # ProxyAgent is appended below with its real semantics; a Router-made
        # one would silently replace the clarification channel.
        if lowered in seen or lowered == "proxyagent":
            continue
        coding_tools = bool(raw.get("coding_tools"))
        use_bing = bool(raw.get("use_bing"))
        # The factory raises on reasoning combined with bing/coding tools;
        # keep the concrete, verifiable capabilities (files, live web) over
        # "reasoning harder".
        use_reasoning = (
            bool(raw.get("use_reasoning")) and not coding_tools and not use_bing
        )
        seen.add(lowered)
        agents.append(
            {
                "input_key": "",
                "type": "",
                "name": name,
                "deployment_name": deployment,
                "icon": "",
                "system_message": str(raw.get("system_message") or "").strip(),
                "description": str(raw.get("description") or "").strip(),
                # No composed team has a Search index; use_rag without
                # index_name yields SearchConfig=None in the factory — an agent
                # that believes it has a knowledge base and does not.
                "use_rag": False,
                "use_mcp": bool(raw.get("use_mcp")),
                "use_bing": use_bing,
                "use_reasoning": use_reasoning,
                "index_name": "",
                "coding_tools": coding_tools,
            }
        )
        if len(agents) >= _COMPOSED_TEAM_MAX_AGENTS:
            break

    if not agents:
        raise ValueError("router roster contained no usable agents")

    # Un workspace montado es parte del contrato de la petición, no una
    # preferencia del Router. El esquema de ``run_plan`` describe ``use_mcp``
    # como "external systems or live data" y no lo exige, así que el Router lo
    # reparte mal para un trabajo sobre el repositorio del propio usuario:
    # medido el 2026-09-19, una vez lo omitió en los cuatro agentes (rev 0000130:
    # auditoría entera fabricada — config.yaml, pyaudio, Dockerfile, nada de eso
    # existe) y otra se lo dio sólo al agente de cloud, dejando ciego al
    # RepositoryForensicsAgent, que era justamente el que debía leer el árbol.
    # Por eso NO es un piso ("que al menos uno vea") sino el contrato completo:
    # el manager Magentic reparte los pasos por nombre y descripción, sin saber
    # quién tiene herramientas, y al agente ciego que le toque mirar el árbol le
    # queda responder de memoria. Sólo añade capacidad; nunca quita la que el
    # Router pidió.
    if workspace_id:
        blind = [a["name"] for a in agents if not a["use_mcp"]]
        for a in agents:
            a["use_mcp"] = True
        if blind:
            logger.info(
                "Workspace %s montado; MCP concedido a %s",
                workspace_id,
                blind,
            )

    # ProxyAgent is the human clarification channel of the Magentic plan; a
    # pattern that runs inside a chat turn (with_proxy=False) has no such slot.
    if with_proxy:
        agents.append(
            {
                "input_key": "",
                "type": "",
                "name": "ProxyAgent",
                "deployment_name": "",
                "icon": "",
                "system_message": "",
                "description": "",
                "use_rag": False,
                "use_mcp": False,
                "use_bing": False,
                "use_reasoning": False,
                "index_name": "",
                "coding_tools": False,
            }
        )

    team_service = TeamService(memory_store)
    team = await team_service.validate_and_parse_team_config(
        {
            "name": "Auto Team",
            # hidden: composed per request — not an entry in the UI picker
            # (_merge_teams_for_direct_response skips hidden the same way).
            "status": "hidden",
            "deployment_name": deployment,
            "description": (
                f"Team composed by the Model Router for: {description[:200]}"
            ),
            "agents": agents,
            "starting_tasks": [
                {
                    "id": "task-1",
                    "name": "Requested task",
                    "prompt": description[:500],
                    "created": "",
                    "creator": "",
                    "logo": "",
                }
            ],
        },
        user_id,
    )
    # Persisted for the plan lane (plan.team_id is resolved later); a team
    # composed for one chat turn lives only as long as the turn.
    if persist:
        await team_service.save_team_configuration(team)
    logger.info(
        "Composed team '%s' (%s) from router roster: %s",
        team.name,
        team.team_id,
        [
            f"{a.name}(code={a.coding_tools},mcp={a.use_mcp},bing={a.use_bing},reason={a.use_reasoning})"
            for a in team.agents
        ],
    )
    return team


async def _create_plan_and_start(
    *,
    background_tasks: BackgroundTasks,
    user_id: str,
    tenant_id: str,
    user_access_token: Optional[str],
    description: str,
    session_id: str,
    history: Optional[list] = None,
    persist_user_task: bool = False,
    composed_agents: Optional[list] = None,
    workspace_id: Optional[str] = None,
) -> str:
    """Create a Plan and kick off the Magentic orchestration as a BackgroundTask.

    Shared core of ``POST /process_request`` and the chat ``run_plan`` escalation
    (the Model Router routing a turn to the formal multi-agent Plan). Returns the
    new ``plan_id``; the orchestration runs after return and streams to PlanPage
    over WebSocket. Raises HTTPException (404 no team / 400 RAI / 500 create).

    ``composed_agents`` is the roster the Model Router proposed in the same
    ``run_plan`` call that escalated the turn. When present and usable, the
    team is composed from it (sanitized + persisted) instead of requiring a
    manually selected team — the roster must exist BEFORE the Magentic graph
    is built, because the graph freezes its participants at ``build()``.
    """
    try:
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        team: Optional[TeamConfiguration] = None
        if composed_agents:
            try:
                team = await _team_from_router_roster(
                    composed_agents,
                    description,
                    user_id,
                    memory_store,
                    workspace_id,
                )
            except Exception as compose_err:
                raise HTTPException(
                    status_code=422,
                    detail=f"Roster del Router inutilizable: {compose_err}",
                ) from compose_err
        if team is None:
            # No roster, no plan. The selected-team / first-available default
            # predates Auto Team and composed blind teams for any task.
            raise HTTPException(
                status_code=422,
                detail="Un plan requiere el roster compuesto por el Router",
            )
        team_id: str | None = team.team_id
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error retrieving team configuration: {e}",
        ) from e

    if not await rai_success(description, team, memory_store):
        track_event_if_configured(
            "Error_RAI_Check_Failed",
            {
                "status": "Plan not created - RAI check failed",
                "description": description,
                "session_id": session_id,
            },
        )
        raise HTTPException(
            status_code=400,
            detail="Request contains content that doesn't meet our safety guidelines, try again.",
        )

    # Recover the SAME session context the router uses (single source of
    # truth). The SSE run_plan escalation passes the already-recovered history;
    # process_request (no prior recovery) recovers it here via the same helper
    # — one loader, not two parallel ones. It rides to run_orchestration and
    # enters the Magentic manager as MagenticContext.chat_history (Messages),
    # never welded into the task string.
    if history is None:
        _ctx_svc = await get_chat_cosmos_service()
        history = await _recover_session_context(
            _ctx_svc, session_id, user_id, description
        )
    input_task = InputTask(
        session_id=session_id,
        description=description,
        workspace_id=workspace_id,
    )

    try:
        plan_id = str(uuid.uuid4())

        if persist_user_task:
            # The plan result IS written back to this session, so without the
            # originating request the history reads as orphan reports with no
            # questions — indistinguishable from duplication. The chat lane
            # already persists the user message before escalating; the direct
            # lane (/process_request) had no writer at all.
            # Written BEFORE the plan document so its timestamp precedes the
            # plan's: the UI orders the canvas by that boundary, and a request
            # must never render below the plan it produced.
            try:
                _chat_svc_q = await get_chat_cosmos_service()
                await _chat_svc_q.add_message(
                    session_id=session_id,
                    user_id=user_id,
                    content=description,
                    role="user",
                    metadata={"intent": "task", "plan_id": plan_id},
                )
            except Exception as _qe:
                logger.warning("Could not persist plan request to chat_cosmos: %s", _qe)

        # Initialize memory store and service
        plan = Plan(
            id=plan_id,
            plan_id=plan_id,
            user_id=user_id,
            session_id=session_id,
            team_id=team_id,
            initial_goal=description,
            overall_status=PlanStatus.in_progress,
        )
        await memory_store.add_plan(plan)

        try:
            _chat_svc = await get_chat_cosmos_service()
            await _chat_svc.add_message(
                session_id=session_id,
                user_id=user_id,
                content="",
                role="assistant",
                metadata={"intent": "task", "plan_id": plan_id},
            )
        except Exception as _e:
            logger.warning("Could not write task anchor to chat_cosmos: %s", _e)

        # Ensure orchestration is initialized before running
        # Force rebuild for each new task since Magentic workflows cannot be reused after completion
        team_service = TeamService(memory_store)
        await OrchestrationManager.get_current_or_new_orchestration(
            user_id=user_id,
            team_config=team,
            team_switched=False,
            team_service=team_service,
            # El workspace que el usuario tiene activo: los agentes lo necesitan
            # para llamar a las tools de MacaeMcpServer con el id real.
            workspace_id=workspace_id,
            force_rebuild=True,  # Always rebuild workflow for new tasks
            # OBO: agents built here run in a BackgroundTask under the app MI unless
            # they carry the user's assertion. Thread it so orchestration agents
            # authenticate as the user (same as direct chat) — required for
            # user-delegated tool connections (e.g. WorkIQ/agent365).
            user_access_token=user_access_token,
        )

        track_event_if_configured(
            "Plan_Created",
            {
                "status": "success",
                "plan_id": plan.plan_id,
                "session_id": session_id,
                "user_id": user_id,
                "team_id": team_id,
                "description": description,
            },
        )
    except Exception as e:
        print(f"Error creating plan: {e}")
        track_event_if_configured(
            "Error_Plan_Creation_Failed",
            {
                "status": "error",
                "description": description,
                "session_id": session_id,
                "user_id": user_id,
                "error": str(e),
            },
        )
        raise HTTPException(status_code=500, detail="Failed to create plan") from e

    try:

        async def run_orchestration_task():
            try:
                await OrchestrationManager().run_orchestration(
                    user_id,
                    session_id,
                    input_task,
                    plan_id=plan_id,
                    history=history,
                    workspace_id=workspace_id,
                )
            finally:
                orchestration_config.clear_run_active(session_id)

        # Mark the session's run in flight BEFORE returning, so a near-immediate
        # resume_plan from PlanPage (orphan recovery on a freshly-created plan)
        # is skipped instead of starting a duplicate orchestration.
        orchestration_config.mark_run_active(session_id)
        background_tasks.add_task(run_orchestration_task)
        return plan_id

    except Exception as e:
        track_event_if_configured(
            "Error_Request_Start_Failed",
            {
                "session_id": session_id,
                "description": description,
                "error": str(e),
            },
        )
        raise HTTPException(
            status_code=400, detail=f"Error starting request: {e}"
        ) from e


# ── Session-aware intent helper ──────────────────────────────────────


async def _get_previous_intent(
    chat_svc: Any,
    session_id: str,
    user_id: str,
) -> Optional[str]:
    """Return the intent of the last assistant message in this session.

    Reads exclusively from chat_cosmos — single source of truth.
    The task anchor is written by process_request at plan-creation time,
    so this function never needs to query any other store.
    """
    try:
        session = await chat_svc.get_session(session_id, user_id)
        if session and session.get("messages"):
            for msg in reversed(session["messages"]):
                if msg.get("role") == "assistant":
                    intent = (msg.get("metadata") or {}).get("intent")
                    if intent:
                        return intent
    except Exception:
        pass
    return None


#: Memoria larga: cuántos resultados pedirle al índice.
RECALL_TOP_K = 15


def _recall_note(hit: dict, content: str) -> str:
    """Un recuerdo se presenta como recuerdo: de qué sesión y de cuándo."""
    nombre = str(hit.get("session_name") or "").strip()
    cuando = str(hit.get("timestamp") or "")[:10]
    quien = "el usuario" if hit.get("role") == "user" else "el asistente"
    origen = f"sesión «{nombre}»" if nombre else "otra sesión"
    return (
        f"[recuerdo de {origen}{f', {cuando}' if cuando else ''}] "
        f"{quien} dijo: {content}"
    )


async def _recover_session_context(
    chat_svc: Any,
    session_id: str,
    user_id: str,
    current_message: str,
) -> list:
    """Rebuild conversation memory from the SINGLE source of truth (Cosmos + AI
    Search) — the SAME recovery the direct-chat path uses, so Plan and chat share
    one context, not two parallel loaders. Two layers, deduped, oldest→newest:
      long memory  → hybrid keyword+vector+semantic retrieval across ALL of the
                     user's history (search_chat_history);
      short memory → this session's turns in order (conversational continuity);
                     an assistant turn that ran tools is followed by a
                     system-attributed evidence note (metadata.turn_log).
    The current user message is not in the doc yet (persisted at turn close).
    Any legacy ``[turn-log]`` trailer inside content is stripped: tool deeds
    never re-enter the model's context as the assistant's own prose.
    """
    history: list = []
    try:
        seen: set = set()
        cur = (current_message or "").strip()
        from common.services.search_index_service import get_search_index_service

        search_svc = await get_search_index_service()
        hits = await search_svc.search_chat_history(
            query=current_message,
            user_id=user_id,
            top_k=RECALL_TOP_K,
        )
        # La memoria larga es RECUERDO, no conversación. Antes entraba con el rol
        # original, así que quince turnos de otras sesiones eran indistinguibles
        # de lo que el usuario acaba de decir y el modelo planificaba con ellos
        # (un roster de agentes de audio para auditar un repo). Misma regla que
        # ya rige para la evidencia de herramientas: atribuida a ``system``.
        # Sin umbral de relevancia: medido contra el índice real, el reranker
        # puntúa 1.9–2.9 tanto a lo pertinente como a lo ajeno, así que un piso
        # no separa nada. Lo que separa es que el recuerdo se vea como recuerdo.
        for h in sorted(hits, key=lambda x: x.get("timestamp", "")):
            # Esta sesión ya entra completa y en orden por la memoria corta.
            if h.get("session_id") and h.get("session_id") == session_id:
                continue
            c = _strip_turn_log_block(h.get("content"))
            if c and c != cur and c not in seen:
                seen.add(c)
                history.append({"role": "system", "content": _recall_note(h, c)})
        session = await chat_svc.get_session(session_id, user_id)
        for m in (session or {}).get("messages", []):
            c = _strip_turn_log_block(m.get("content"))
            if c and c != cur and c not in seen:
                seen.add(c)
                history.append({"role": m.get("role", "user"), "content": c})
            # Evidencia de ejecución del turno (metadata.turn_log), atribuida
            # al sistema y pegada al turno del asistente que la produjo. Sólo
            # el doc de sesión la tiene (Search indexa role/content/timestamp).
            if m.get("role") == "assistant":
                _meta = m.get("metadata") or {}
                note = _tool_deeds_note(
                    _meta.get("turn_log"), _meta.get("turn_log_dropped")
                )
                if note:
                    history.append({"role": "system", "content": note})
    except Exception as _hist_err:
        logger.warning("Could not rebuild chat history: %s", _hist_err)
    return history


# ── Chat Mode Endpoint (P0 — conversational without plan) ────────────


@app_v4.post("/chat/upload-file")
async def chat_upload_file(
    request: Request,
    file: UploadFile = File(...),
):
    """
    Upload a file to Azure AI Foundry for use with code_interpreter.

    Returns a file_id to include in the subsequent chat/message/stream request
    via the file_ids field. Foundry attaches it to the thread message, making
    it available for code_interpreter to read and process.

    ---
    tags:
      - Chat
    """
    from azure.ai.agents.aio import AgentsClient

    from common.config.app_config import config as app_config

    _extract_auth_with_token(request)
    # Borrowed process-scoped credential (closed once in the lifespan).
    # Minting one per request leaked its aiohttp ClientSession: async
    # credentials own a session and nothing here ever closed it.
    creds = app_config.get_shared_async_credential()

    contents = await file.read()
    filename = file.filename or "upload"

    # No size judgement here: this endpoint is transport. Whether the file has
    # 0 bytes or 3000 is content, and reading content is the code interpreter's
    # job — rejecting an empty upload also denied the model the chance to say
    # "this file is empty", which is an answer the user asked for.
    logger.info(
        "Uploading file to Foundry: name=%s size=%d bytes content_type=%s",
        filename,
        len(contents),
        file.content_type,
    )

    try:
        async with AgentsClient(
            endpoint=app_config.AZURE_AI_PROJECT_ENDPOINT,
            credential=creds,
        ) as agents_client:
            # Pass a (filename, bytes, content_type) tuple — same pattern used by
            # setup_search_pipeline.py: send raw bytes with explicit filename so
            # Foundry can identify the file and avoids "File is empty" errors.
            mime = file.content_type or "application/octet-stream"
            uploaded = await agents_client.files.upload(
                file=(filename, contents, mime),
                purpose="assistants",
            )

            logger.info(
                "File uploaded to Foundry: file_id=%s name=%s size=%d bytes",
                uploaded.id,
                filename,
                len(contents),
            )
            return {"file_id": uploaded.id, "filename": filename, "size": len(contents)}
    except Exception as ex:
        logger.error("File upload to Foundry failed: %s", ex)
        raise HTTPException(status_code=500, detail=f"File upload failed: {ex}")


# ── Generated-file persistence plumbing ──────────────────────────────
# Fire-and-forget with a strong reference (unreferenced tasks can be GC'd
# mid-flight) and exception retrieval (else asyncio logs "exception was
# never retrieved" at teardown).
_BG_PERSIST_TASKS: set = set()


def _spawn_bg_persist(coro, label: str) -> None:
    task = asyncio.create_task(coro)
    _BG_PERSIST_TASKS.add(task)

    def _done(t) -> None:
        _BG_PERSIST_TASKS.discard(t)
        exc = None if t.cancelled() else t.exception()
        if exc:
            logger.error("Background persist %s failed: %s", label, exc)

    task.add_done_callback(_done)


def _file_response(data: bytes, filename: str):
    """Bytes → streaming-friendly file Response with inferred content type."""
    import mimetypes

    from fastapi.responses import Response

    mime, _ = mimetypes.guess_type(filename)
    mime = mime or "application/octet-stream"
    previewable_prefixes = ("image/", "text/")
    previewable_mimes = {"application/pdf"}
    disposition = (
        "inline"
        if mime in previewable_mimes or mime.startswith(previewable_prefixes)
        else "attachment"
    )
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Content-Length": str(len(data)),
            "Cache-Control": "private, max-age=86400, immutable",
        },
    )


class HtmlPreviewRequest(BaseModel):
    """Model-generated HTML to publish as an isolated-origin preview."""

    html: str
    title: str = ""


@app_v4.post("/chat/preview")
async def create_html_preview(request: Request, body: HtmlPreviewRequest):
    """Publish model-generated HTML on the Blob origin and return its SAS URL.

    The preview iframe needs a REAL origin (location/history/hash routing and
    storage all dead in an opaque srcdoc origin). The storage account is that
    origin: distinct from frontend and backend in dev and prod, no cookies or
    ambient credentials — ``allow-same-origin`` there never means MACAE.
    """
    user_id, _tenant_id = _extract_auth(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user identity")
    if len(body.html) > 2_000_000:
        raise HTTPException(status_code=413, detail="Preview HTML too large")

    from v4.common.services.generated_file_store import GeneratedFileStore

    blob_name = f"preview_{uuid.uuid4().hex}.html"
    url = await GeneratedFileStore.get_instance().save_preview_html(
        blob_name, body.html
    )
    if not url:
        raise HTTPException(status_code=502, detail="Preview publish failed")
    return {"url": url}


@app_v4.get("/chat/download-file/{file_id}")
async def chat_download_file(
    request: Request,
    file_id: str,
    container_id: str | None = None,
):
    """
    Download a file generated by code_interpreter (Assistants API).

    The file_id comes from annotations[].file_id emitted in the SSE stream
    as a ``generated_file`` event. The file bytes are retrieved from Foundry
    using AgentsClient and streamed back to the caller.

    ---
    tags:
      - Chat
    """
    from common.config.app_config import config as app_config
    from v4.common.services.generated_file_store import GeneratedFileStore

    # Blob first: files persisted at generation time outlive their Foundry
    # container. A miss means the file predates the store → live-Foundry path.
    stored = await GeneratedFileStore.get_instance().load(file_id)
    if stored is not None:
        data, filename = stored
        return _file_response(data, filename)

    try:
        if file_id.startswith("cfile_"):
            if not container_id:
                raise HTTPException(
                    status_code=400,
                    detail="container_id is required to download generated container files",
                )

            # The container was created by the chat's direct Responses API call
            # ({account}/openai, api-version 2025-03-01-preview). It must be read
            # back through the SAME endpoint + identity: the project client
            # (get_openai_client) targets a different scope and 404s ("Container
            # not found"), and a user-scoped container is invisible to a
            # different identity — so mirror _RouterChatClient's OBO bearer.
            from openai import AsyncOpenAI, NotFoundError

            access_token = None
            if request is not None:
                try:
                    _, _, access_token = _extract_auth_with_token(request)
                except Exception:
                    access_token = None
            _obo_cred = None
            try:
                if access_token and app_config.ENABLE_OBO:
                    _obo_cred = app_config.build_user_credential(access_token)
                    bearer = (
                        await _obo_cred.get_token("https://ai.azure.com/.default")
                    ).token
                else:
                    bearer = (
                        await app_config.get_shared_async_credential().get_token(
                            "https://ai.azure.com/.default"
                        )
                    ).token
                account = (app_config.AZURE_AI_PROJECT_ENDPOINT or "").split(
                    "/api/projects/"
                )[0]
                openai = AsyncOpenAI(
                    api_key=bearer,
                    base_url=f"{account}/openai",
                    default_query={"api-version": _DIRECT_RESPONSES_API_VERSION},
                    timeout=60,
                )
                try:
                    file_info: Any = await openai.containers.files.retrieve(
                        file_id=file_id,
                        container_id=container_id,
                    )
                    file_path = getattr(file_info, "path", None) or file_id
                    filename = (
                        os.path.basename(file_path) if file_path != file_id else file_id
                    )
                    content = await openai.containers.files.content.retrieve(
                        file_id=file_id,
                        container_id=container_id,
                    )
                    data = await content.aread()
                except NotFoundError as nf:
                    # Container recycled (~20 min) and the file was never
                    # persisted (predates the store). Terminal state: the
                    # bytes no longer exist anywhere.
                    raise HTTPException(
                        status_code=410,
                        detail=(
                            "Generated file expired with its Foundry container "
                            "before persistence existed"
                        ),
                    ) from nf
                finally:
                    await openai.close()
            finally:
                if _obo_cred is not None:
                    try:
                        await _obo_cred.close()
                    except Exception:
                        pass
        else:
            from azure.ai.agents.aio import AgentsClient

            # Borrowed shared credential — per-request minting leaked the
            # credential's aiohttp ClientSession (nothing closed it).
            creds = app_config.get_shared_async_credential()
            async with AgentsClient(
                endpoint=app_config.AZURE_AI_PROJECT_ENDPOINT,
                credential=creds,
            ) as agents_client:
                file_info = await agents_client.files.get(file_id)
                filename = getattr(file_info, "filename", None) or file_id

                content_stream = await agents_client.files.get_content(file_id)
                chunks = []
                async for chunk in content_stream:
                    chunks.append(bytes(chunk))
                data = b"".join(chunks)

        # Backfill: a pre-store file just fetched from live Foundry gets
        # persisted so the NEXT read outlives the container.
        _spawn_bg_persist(
            GeneratedFileStore.get_instance().save(file_id, filename, data),
            f"backfill:{file_id}",
        )
        return _file_response(data, filename)
    except HTTPException:
        raise  # preserve the original status code (e.g. 400 for missing container_id)
    except ResourceNotFoundError as nf:
        # The AgentsClient branch above raises azure-core's ResourceNotFoundError,
        # not openai's NotFoundError, so it never hit the 410 handler and fell
        # through to the generic 500 below — a missing file reported as a server
        # fault. 404 is the truth here, and the schema already declares it.
        logger.info("Generated file not found in Foundry: file_id=%s", file_id)
        raise HTTPException(
            status_code=404, detail=f"Generated file '{file_id}' not found"
        ) from nf
    except Exception as ex:
        logger.error(
            "File download from Foundry failed: file_id=%s error=%s", file_id, ex
        )
        raise HTTPException(status_code=500, detail=f"File download failed: {ex}")


@app_v4.post("/chat/message")
async def chat_message(
    background_tasks: BackgroundTasks,
    chat_request: ChatMessageRequest,
    request: Request,
):
    """
    Handle a chat message with intent classification.

    Routes messages to the appropriate handler:
    - "task" → Redirects to process_request (full plan workflow)
    - "conversational" → Direct agent response without plan creation
    - "mcp_query" → MCP Inspector / bridge query

    ---
    tags:
      - Chat
    """
    from v4.orchestration.intent_router import Intent, IntentRouter

    user_id, tenant_id, user_access_token = _extract_auth_with_token(request)

    # Assign session_id if not provided
    if not chat_request.session_id:
        chat_request.session_id = str(uuid.uuid4())

    # ── Persist user message to Cosmos DB ────────────────────────
    chat_svc = await get_chat_cosmos_service()
    try:
        await chat_svc.add_message(
            session_id=chat_request.session_id,
            user_id=user_id,
            content=chat_request.message,
            role="user",
        )
    except Exception as e:
        logger.warning("Could not persist user chat message: %s", e)

    previous_intent = await _get_previous_intent(
        chat_svc, chat_request.session_id, user_id
    )

    # Front-door decision first. Do not invoke a worker agent before deciding
    # whether this request should create a formal plan.
    intent_result = await IntentRouter.classify_async(
        chat_request.message,
        previous_intent=previous_intent,
    )
    logger.info(
        "Chat intent: %s (confidence=%.2f, prev=%s) for message: %s",
        intent_result.intent.value,
        intent_result.confidence,
        previous_intent,
        chat_request.message[:80],
    )

    # Never create a new plan when the message originates from an open plan OR
    # the UI selector is in Chat position (allow_plan=False).
    if (
        chat_request.plan_id or not chat_request.allow_plan
    ) and intent_result.intent == Intent.TASK:
        logger.info(
            "plan_id=%s allow_plan=%s — downgrading TASK to CONVERSATIONAL",
            chat_request.plan_id,
            chat_request.allow_plan,
        )
        from v4.orchestration.intent_router import IntentResult

        intent_result = IntentResult(
            intent=Intent.CONVERSATIONAL,
            confidence=intent_result.confidence,
            reasoning="in-plan follow-up or chat-only selector — not a new task",
        )

    # ── Route by intent ──────────────────────────────────────────
    if intent_result.intent == Intent.TASK:
        input_task_for_plan = InputTask(
            session_id=chat_request.session_id,
            description=chat_request.message,
            workspace_id=chat_request.workspace_id,
        )
        try:
            result = await process_request(
                background_tasks, input_task_for_plan, request
            )
            # process_request already wrote the task anchor to chat_cosmos.
            return ChatMessageResponse(
                session_id=chat_request.session_id,
                intent="task",
                confidence=intent_result.confidence,
                response="I've created a plan for your request. Redirecting to plan view.",
                agent="planner",
                redirect_to_plan=result.get("plan_id"),
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Error creating plan from chat: %s", e)
            raise HTTPException(
                status_code=500, detail=f"Error creating plan: {e}"
            ) from e

    else:
        actual_intent = intent_result.intent.value  # "mcp_query" or "conversational"
        agent_response = await _get_mcp_query_response(
            chat_request.message,
            chat_request.session_id,
            user_id,
            chat_svc,
            tenant_id=tenant_id,
            user_access_token=user_access_token,
        )
        response_text = agent_response

        # Persist assistant response with the precise intent label
        try:
            await chat_svc.add_message(
                session_id=chat_request.session_id,
                user_id=user_id,
                content=response_text,
                role="assistant",
                metadata={"intent": actual_intent},
            )
        except Exception as e:
            logger.warning("Could not persist %s response: %s", actual_intent, e)

        track_event_if_configured(
            f"Chat_{actual_intent}",
            {
                "session_id": chat_request.session_id,
                "user_id": user_id,
                "message": chat_request.message[:200],
            },
        )

        return ChatMessageResponse(
            session_id=chat_request.session_id,
            intent=actual_intent,
            confidence=intent_result.confidence,
            response=response_text,
            agent="assistant",
        )


# ── Streaming Chat Endpoint (SSE) ────────────────────────────────


def _sse_event(data: dict) -> str:
    """Format a dict as an SSE data event."""
    return f"data: {json.dumps(data)}\n\n"


def _safe_json_dumps(value: Any) -> str:
    """Safely serialize arbitrary SDK objects for logs/SSE previews."""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _to_safe_dict(value: Any, max_depth: int = 4, _depth: int = 0) -> Any:
    """Best-effort conversion of SDK event objects to JSON-safe structures."""
    if _depth >= max_depth:
        return str(value)

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {
            str(k): _to_safe_dict(v, max_depth=max_depth, _depth=_depth + 1)
            for k, v in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [_to_safe_dict(v, max_depth=max_depth, _depth=_depth + 1) for v in value]

    # Pydantic-like models
    for attr in ("model_dump", "dict"):
        meth = getattr(value, attr, None)
        if callable(meth):
            try:
                dumped = meth()
                return _to_safe_dict(dumped, max_depth=max_depth, _depth=_depth + 1)
            except Exception:
                pass

    # Generic object attributes
    try:
        if hasattr(value, "__dict__"):
            return _to_safe_dict(
                {
                    k: v
                    for k, v in vars(value).items()
                    if not str(k).startswith("_") and not callable(v)
                },
                max_depth=max_depth,
                _depth=_depth + 1,
            )
    except Exception:
        pass

    return str(value)


def _extract_function_result_payload(item: Any) -> dict[str, Any]:
    """Extract rich tool-result payload from response output items."""
    safe_item = _to_safe_dict(item)
    payload: dict[str, Any] = {
        "type": getattr(item, "type", None),
        "call_id": getattr(item, "call_id", None),
        "status": getattr(item, "status", None),
        "name": getattr(item, "name", None),
        "arguments": getattr(item, "arguments", None),
    }

    # Common payload-bearing fields across SDK/event variants
    for field in (
        "output",
        "result",
        "content",
        "text",
        "value",
        "message",
        "stdout",
        "stderr",
    ):
        val = getattr(item, field, None)
        if val not in (None, ""):
            payload[field] = _to_safe_dict(val)

    # Include the raw normalized snapshot for forward compatibility/debugging
    payload["raw"] = safe_item
    return payload


def _is_invokable_agent(agent: Any) -> bool:
    return callable(getattr(agent, "invoke", None))


def _agent_runtime_name(agent: Any) -> str:
    return getattr(agent, "agent_name", None) or getattr(agent, "name", "") or ""


def _agent_config_by_name(team: Any) -> dict[str, Any]:
    return {
        (getattr(agent, "name", "") or "").lower(): agent
        for agent in getattr(team, "agents", []) or []
        if getattr(agent, "name", "")
    }


def _build_agent_description(agent: Any, config: Any) -> str:
    """Build a rich description for an agent, including its capabilities."""
    description = getattr(config, "description", "") or ""
    capabilities = []
    if getattr(config, "use_rag", False):
        index_name = getattr(config, "index_name", "") or ""
        capabilities.append(
            f"RAG/Search{f'(index={index_name})' if index_name else ''}"
        )
    if getattr(config, "use_mcp", False):
        capabilities.append("MCP/external-tools")
    if getattr(config, "use_reasoning", False):
        capabilities.append("Reasoning")
    if getattr(config, "coding_tools", False):
        capabilities.append("CodeInterpreter")
    if capabilities:
        description = f"{description} [capabilities: {', '.join(capabilities)}]".strip()
    return description or "(no description)"


def _match_agent_by_name(chosen: str, agents: list[Any]) -> Optional[Any]:
    """Fuzzy-match LLM response to an agent.

    Tries (in order):
    1. Exact case-insensitive match on the full name.
    2. The chosen string is contained in the agent name (handles trailing punctuation / spacing).
    3. The agent name is contained in the chosen string (handles extra explanation text).
    Returns None if no match found.
    """
    chosen_stripped = chosen.strip().rstrip(".,;:!?").lower()
    for a in agents:
        name = (_agent_runtime_name(a) or "").lower()
        if name == chosen_stripped:
            return a
    for a in agents:
        name = (_agent_runtime_name(a) or "").lower()
        if chosen_stripped in name or name in chosen_stripped:
            return a
    return None


async def _select_team_agent(message: str, team: Any, agents: list[Any]) -> Any:
    """Select the best agent using ProxyAgent as the primary intermediary.

    ProxyAgent acts as the central coordinator between the user and all
    specialized agents. It receives the user request, understands the context,
    and delegates tasks to the appropriate specialist agents (TechnicalSupportAgent,
    HRHelperAgent, MarketingAgent, etc.) as needed.

    Selection strategy:
    1. If a ProxyAgent is available, always prefer it — it is the designated
       intermediary that maintains conversation context and routes internally.
    2. If there is only one invokable agent, return it directly.
    3. Fall back to LLM-based routing only when no ProxyAgent is present and
       multiple specialist agents are available.
    """
    from common.config.app_config import config as app_config

    invokable_agents = [agent for agent in agents if _is_invokable_agent(agent)]
    if not invokable_agents:
        return None

    if len(invokable_agents) == 1:
        return invokable_agents[0]

    # Prefer ProxyAgent as the central intermediary/orchestrator
    for agent in invokable_agents:
        name = (_agent_runtime_name(agent) or "").lower()
        if name == "proxyagent":
            logger.info(
                "Routing message through ProxyAgent (central intermediary) for team '%s'",
                getattr(team, "name", "unknown"),
            )
            return agent

    # No ProxyAgent found — fall back to LLM-based routing among specialist agents
    logger.info(
        "No ProxyAgent found in team '%s'; using LLM router across %d agents.",
        getattr(team, "name", "unknown"),
        len(invokable_agents),
    )

    configs = _agent_config_by_name(team)
    agent_lines = []
    for a in invokable_agents:
        name = _agent_runtime_name(a) or ""
        cfg = configs.get(name.lower())
        desc = _build_agent_description(a, cfg) if cfg else "(no description)"
        agent_lines.append(f"- {name}: {desc}")
    agent_list = "\n".join(agent_lines)

    prompt = (
        "You are an agent router. Given the list of agents and a user message, "
        "reply with ONLY the exact name of the single agent that best handles the request. "
        "No explanation, no punctuation — just the agent name.\n\n"
        f"Agents:\n{agent_list}\n\n"
        f"User message: {message}\n\n"
        "Agent name:"
    )

    chosen_agent: Optional[Any] = None
    try:
        project = app_config.get_ai_project_client()
        try:
            openai = project.get_openai_client()
            try:
                resp = await openai.chat.completions.create(
                    model=app_config.AZURE_OPENAI_DEPLOYMENT_NAME,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=30,
                    temperature=0,
                )
                chosen_text = (resp.choices[0].message.content or "").strip()
                logger.info("LLM router raw response: %r", chosen_text)
                chosen_agent = _match_agent_by_name(chosen_text, invokable_agents)
                if chosen_agent is None:
                    logger.warning(
                        "LLM router returned unrecognised agent name %r; "
                        "falling back to first agent. Available: %s",
                        chosen_text,
                        [_agent_runtime_name(a) for a in invokable_agents],
                    )
            finally:
                await openai.close()
        finally:
            if hasattr(project, "close"):
                await project.close()
    except Exception as exc:
        logger.warning("LLM router failed (%s); falling back to first agent.", exc)

    return chosen_agent or invokable_agents[0]


def _team_agent_context(team: Any) -> str:
    lines = []
    team_name = getattr(team, "name", "") or "Current team"
    team_description = getattr(team, "description", "") or ""
    lines.append(f"Current team: {team_name}")
    if team_description:
        lines.append(f"Team description: {team_description}")
    lines.append("Available team agents:")
    for agent in getattr(team, "agents", []) or []:
        name = getattr(agent, "name", "") or "UnnamedAgent"
        description = getattr(agent, "description", "") or ""
        capabilities = []
        if getattr(agent, "use_rag", False):
            index_name = getattr(agent, "index_name", "") or ""
            capabilities.append(
                f"RAG/Search{f' index={index_name}' if index_name else ''}"
            )
        if getattr(agent, "use_reasoning", False):
            capabilities.append("Reasoning")
        if getattr(agent, "use_mcp", False):
            capabilities.append("MCP")
        if getattr(agent, "coding_tools", False):
            capabilities.append("Code Interpreter")
        suffix = f" ({', '.join(capabilities)})" if capabilities else ""
        lines.append(f"- {name}{suffix}: {description}")
    return "\n".join(lines)


def _build_direct_chat_prompt(message: str, team: Any, selected_agent: Any) -> str:
    selected_name = _agent_runtime_name(selected_agent) or "UnknownAgent"
    return (
        "ROUTING CONTEXT FROM MACAE BACKEND\n"
        f"Selected responding agent for this turn: {selected_name}\n"
        f"{_team_agent_context(team)}\n\n"
        "Instructions:\n"
        "- If the user asks which agent is responding, answer with the selected "
        "responding agent name above.\n"
        "- Do not say you lack access to the current agent/team when the routing "
        "context provides it.\n"
        "- For domain questions, use your configured tools/knowledge as usual.\n"
        "- Respond in the user's language.\n\n"
        f"USER MESSAGE:\n{message}"
    )


def _get_m_plan_id_from_plan(plan: Any) -> Optional[str]:
    m_plan = getattr(plan, "m_plan", None)
    if isinstance(m_plan, dict):
        return m_plan.get("id") or m_plan.get("m_plan_id")
    return getattr(m_plan, "id", None) or getattr(m_plan, "m_plan_id", None)


def _build_plan_chat_prompt(
    message: str,
    team: Any,
    selected_agent: Any,
    plan: Any,
) -> str:
    selected_name = _agent_runtime_name(selected_agent) or "UnknownAgent"
    m_plan = getattr(plan, "m_plan", None)
    if m_plan and hasattr(m_plan, "model_dump"):
        m_plan = m_plan.model_dump()

    return (
        "ACTIVE PLAN CONTEXT FROM MACAE BACKEND\n"
        f"plan_id: {getattr(plan, 'plan_id', '') or getattr(plan, 'id', '')}\n"
        f"m_plan_id: {_get_m_plan_id_from_plan(plan) or ''}\n"
        f"plan_status: {getattr(plan, 'overall_status', '')}\n"
        f"initial_goal: {getattr(plan, 'initial_goal', '')}\n"
        f"Selected responding agent for this turn: {selected_name}\n"
        f"{_team_agent_context(team)}\n\n"
        "Current m_plan object, if available:\n"
        f"{json.dumps(m_plan, ensure_ascii=False, default=str) if m_plan else '{}'}\n\n"
        "Instructions:\n"
        "- This message is inside the active application Plan context above.\n"
        "- Do not create a new application Plan for this message.\n"
        "- Use the active plan state and team as authoritative context.\n"
        "- If the user is asking about status, next steps, approval, or prior work, "
        "answer in relation to the active plan.\n"
        "- Respond in the user's language.\n\n"
        f"USER MESSAGE:\n{message}"
    )


def _agent_can_join_direct_orchestration(agent: Any) -> bool:
    """Return True for agents that implement the AgentFramework protocol."""
    return callable(getattr(agent, "run", None)) or callable(
        getattr(agent, "invoke", None)
    )


def _merge_teams_for_direct_response(
    teams: list[TeamConfiguration],
    user_id: str,
    default_deployment_name: str,
) -> TeamConfiguration:
    """Build a direct-response team from all available teams.

    ProxyAgent is intentionally kept once with its original clarification role.
    Business agents keep their names so the Magentic manager can route to the
    same participants users see elsewhere in the app.
    """
    merged_agents: list[TeamAgent] = []
    seen_names: set[str] = set()
    descriptions: list[str] = []

    for team in teams:
        if getattr(team, "status", "visible") == "hidden":
            continue
        team_name = getattr(team, "name", "") or "Unnamed Team"
        team_description = getattr(team, "description", "") or ""
        descriptions.append(f"{team_name}: {team_description}".strip())

        for agent in getattr(team, "agents", []) or []:
            name = getattr(agent, "name", "") or ""
            if not name:
                continue
            normalized = name.lower()
            if normalized == "proxyagent":
                if "proxyagent" in seen_names:
                    continue
                seen_names.add("proxyagent")
                merged_agents.append(agent)
                continue
            if normalized in seen_names:
                logger.warning(
                    "Skipping duplicate direct-response agent name '%s' from team '%s'",
                    name,
                    team_name,
                )
                continue
            seen_names.add(normalized)

            agent_dict = (
                agent.model_dump() if hasattr(agent, "model_dump") else dict(agent)
            )
            agent_dict["description"] = (
                f"[Team: {team_name}] {agent_dict.get('description', '')}".strip()
            )
            agent_dict["system_message"] = (
                f"Team context: {team_name}. {team_description}\n\n"
                f"{agent_dict.get('system_message', '')}"
            ).strip()
            merged_agents.append(TeamAgent(**agent_dict))

    if not any((agent.name or "").lower() == "proxyagent" for agent in merged_agents):
        merged_agents.append(
            TeamAgent(
                input_key="",
                type="",
                name="ProxyAgent",
                deployment_name=default_deployment_name,
                icon="",
                system_message="",
                description="Clarification agent for missing user details.",
            )
        )

    return TeamConfiguration(
        team_id="direct-response-team",
        name="Direct Response Team",
        status="visible",
        created="",
        created_by=user_id,
        deployment_name=default_deployment_name,
        agents=merged_agents,
        description=(
            "Direct response orchestration across all available teams. "
            + "\n".join(descriptions)
        ),
        logo="",
        plan="",
        starting_tasks=[],
        user_id=user_id,
    )


_DIRECT_RESPONSES_API_VERSION = "2025-03-01-preview"


# ── Composer ────────────────────────────────────────────────────────────────
# The orchestrations the framework offers (agent_framework_orchestrations
# builders), in the composer's terms. The composer picks one from the request
# and the conversation; nothing in code inspects the request.
_PATTERNS: dict[str, str] = {
    "magentic": (
        "One composite objective spanning clearly separate domains of "
        "responsibility, coordinated by a manager that plans, keeps a progress "
        "ledger, re-plans on stalls and submits the plan for human review before "
        "executing (the formal Plan, tracked on its own page)."
    ),
    "group_chat": (
        "Open collaboration: an orchestrator picks who speaks next from the "
        "conversation state until the group converges."
    ),
    "sequential": (
        "A fixed pipeline: each specialist builds on the previous one's output, "
        "in the order given."
    ),
    "concurrent": (
        "Independent specialists work on the same request in parallel and their "
        "outputs are aggregated."
    ),
    "handoff": (
        "One specialist owns the conversation with the user and hands it to "
        "another specialist when the topic leaves its domain."
    ),
}

# A participant: the same schema the plan lane materializes with
# ``_team_from_router_roster`` (factory constraints re-checked there, in code).
# Each boolean is a flag of the team model that the factory turns into a tool of
# the participant's template — the composer names the capability, the template
# attaches it.
_PARTICIPANT_SCHEMA: dict = {
    "type": "array",
    "description": (
        "The specialists for this request — 1 to 4, fewer is better; derive them "
        "from the request itself. Specialists are REUSED by name across requests, "
        "so write system_message as reusable role instructions (what the "
        "specialist is and does), never one-task orders. Do not include a proxy, "
        "manager or orchestrator entry."
    ),
    "items": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "PascalCase, ending in 'Agent', unique in the roster (e.g. DataAgent)."
                ),
            },
            "description": {
                "type": "string",
                "description": "One line: what this specialist is for.",
            },
            "system_message": {
                "type": "string",
                "description": "Reusable role instructions for the specialist.",
            },
            "coding_tools": {
                "type": "boolean",
                "description": (
                    "true ONLY if it must run code or produce real downloadable "
                    "files (scripts, spreadsheets, packages, charts)."
                ),
            },
            "use_mcp": {
                "type": "boolean",
                "description": "true ONLY if it needs external systems or live data.",
            },
            "use_bing": {
                "type": "boolean",
                "description": (
                    "true ONLY if it needs LIVE public web information (current "
                    "prices, news, market data). Never together with use_reasoning."
                ),
            },
            "use_reasoning": {
                "type": "boolean",
                "description": (
                    "Deep multi-step analysis. Never together with coding_tools."
                ),
            },
        },
        "required": ["name", "description", "system_message"],
    },
}

_COMPOSER_INSTRUCTIONS = (
    "You are the orchestration layer of a multi-agent system built on Microsoft "
    "Agent Framework. You receive the user's request with the conversation it "
    "belongs to (this session's turns, plus earlier turns retrieved from the "
    "user's history). Either answer the request yourself or call `compose` once "
    "to run it through one of the framework's orchestrations with the "
    "specialists it needs. Decide from the request and the conversation."
)


def _compose_tool(patterns: list[str]) -> dict:
    """The composer's single function, as a Responses function tool.

    ``patterns`` restricts the enum: the Plan position passes ``["magentic"]``
    (the human asked for a plan) and an in-plan chat turn leaves it out.
    """
    return {
        "type": "function",
        "name": "compose",
        "description": (
            "Run the request through an orchestration of specialists. Call it "
            "once; do not call it when you answer the request yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "enum": list(patterns),
                    "description": " ".join(
                        f"{name}: {_PATTERNS[name]}" for name in patterns
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "The full, self-contained objective, as the user expressed it."
                    ),
                },
                "participants": _PARTICIPANT_SCHEMA,
            },
            "required": ["pattern", "task", "participants"],
        },
    }


def _read_composition(
    arguments: Optional[str], patterns: list[str]
) -> tuple[str, str, list[dict]]:
    """``compose`` arguments -> ``(pattern, task, participants)``.

    Raises ``ValueError`` when the arguments are not a usable composition; the
    participants are sanitized later by ``_team_from_router_roster``, never
    trusted as-is.
    """
    try:
        args = json.loads(arguments or "{}")
    except ValueError as e:
        raise ValueError(f"compose arguments are not JSON: {e}") from e
    if not isinstance(args, dict):
        raise ValueError("compose arguments are not an object")
    pattern = str(args.get("pattern") or "").strip().lower()
    if pattern not in patterns:
        raise ValueError(f"unknown orchestration pattern '{pattern}'")
    participants = args.get("participants")
    return (
        pattern,
        str(args.get("task") or "").strip(),
        [
            p
            for p in (participants if isinstance(participants, list) else [])
            if isinstance(p, dict)
        ],
    )


class _RouterChatClient:
    """Front door of the chat lane: o4-mini on the direct Responses API.

    One reasoning model reads the request with its conversation and either
    answers it, streamed as text, or calls the single function ``compose`` with
    the orchestration the framework offers, the participants and the
    capabilities each one needs. Nothing here executes a capability: the
    existing factory builds the participants (``FoundryAgentTemplate`` attaches
    each one's tools) and the framework builder of the chosen pattern runs
    them. ``magentic`` is the formal Plan and leaves this turn through
    ``composition`` (process_request's path: Magentic manager, plan review,
    WebSocket, PlanPage); the other patterns run inside this turn and their
    agents' updates stream through the same SSE handler.

    The Model Router is not in this layer any more: it is chat/completions-only,
    cannot carry tools and is not an agent model, so it never belonged at the
    orchestration decision. This class keeps ``FoundryAgentTemplate.invoke()``'s
    contract — yields updates exposing ``.contents`` — so the SSE handler is
    reused.
    """

    def __init__(
        self,
        agent_name: str,
        user_access_token: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        memory_store: Any = None,
    ) -> None:
        from common.config.app_config import config

        self.agent_name = agent_name
        self._workspace_id = workspace_id
        # End-user access token (EasyAuth/Bearer): the composer call runs
        # on-behalf-of the user when OBO is provisioned, and the participants'
        # MCP tools forward it (lifecycle._prepare_mcp_tool).
        self._user_access_token = user_access_token
        self._user_id = user_id or ""
        self._user_cred = None
        # Needed to materialize a composed team and build its agents when a
        # pattern runs inside the turn; the Plan position does not run
        # participants here and may omit it.
        self._memory_store = memory_store
        # What the composer decided this turn: None when it answered directly,
        # else (pattern, task, participants). The SSE handler reads it after the
        # stream to create the formal Plan when the pattern is ``magentic``.
        self.composition: Optional[tuple[str, str, list[dict]]] = None
        # AZURE_AI_PROJECT_ENDPOINT is {account}/api/projects/{project}. The
        # model's OpenAI-compatible Responses API lives at the ACCOUNT root
        # ({account}/openai, api-version 2025-03-01-preview — the first version
        # that enables the Responses API).
        project = (config.AZURE_AI_PROJECT_ENDPOINT or "").rstrip("/")
        account = project.split("/api/projects/")[0]
        self._openai_base_url = f"{account}/openai"
        self._api_version = _DIRECT_RESPONSES_API_VERSION
        self._model = config.CHAT_ORCHESTRATOR_MODEL

    async def _bearer(self) -> str:
        from common.config.app_config import config

        # Prefer the end user's identity (OBO) ONLY when OBO is provisioned.
        # The ENABLE_OBO gate is deliberate: without it, build_user_credential
        # returns a passthrough of the raw user token (wrong audience for the
        # Foundry data plane) which would break the call. In local dev
        # (ENABLE_OBO off) we fall through to the shared credential, which
        # resolves to the developer's az-login user anyway — so dev keeps working.
        if self._user_access_token and config.ENABLE_OBO:
            if self._user_cred is None:
                self._user_cred = config.build_user_credential(self._user_access_token)
            _cred_bearer = self._user_cred
            if _cred_bearer is not None:
                token = await _cred_bearer.get_token("https://ai.azure.com/.default")
                return token.token

        # No user token / OBO not enabled: borrow the process-shared credential
        # (app identity in prod, az-login user in dev). Never closed here — owned
        # by the app lifespan.
        cred = config.get_shared_async_credential()
        token = await cred.get_token("https://ai.azure.com/.default")
        return token.token

    def _responses_client(self, bearer: str):
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=bearer,
            base_url=self._openai_base_url,
            default_query={"api-version": self._api_version},
            timeout=120,
        )

    def _instructions(self) -> str:
        """The composer's instructions plus the facts of this conversation it
        cannot infer: a mounted workspace is readable only through specialists
        with ``use_mcp`` (the composer itself has no tools)."""
        if not self._workspace_id:
            return _COMPOSER_INSTRUCTIONS
        return (
            f"{_COMPOSER_INSTRUCTIONS}\n\nWORKSPACE: the user's project workspace "
            f"'{self._workspace_id}' is mounted for this conversation. Only "
            "specialists composed with use_mcp=true can list, read and search its "
            "files and run read-only git on it (MacaeMcpServer workspace tools); "
            "you have no tools yourself, so you cannot see its contents."
        )

    @staticmethod
    def _composer_input(prompt: str, history: Optional[list]) -> list:
        # Memory = the conversation itself, rebuilt from Cosmos + AI Search and
        # passed as input message items (NOT previous_response_id); store=False
        # on every call, nothing is threaded server-side.
        return list(history or []) + [{"role": "user", "content": prompt}]

    def _text_update(self, text: str) -> AgentResponseUpdate:
        """The composer's own words, in the framework's update type."""
        return AgentResponseUpdate(
            contents=[Content.from_text(text)],
            role="assistant",
            author_name=self.agent_name,
        )

    async def compose_plan(self, prompt: str) -> tuple[str, str, list]:
        """Plan position: ``compose`` forced with ``pattern=magentic``.

        The human asked for a plan; the composer decides the task and the
        participants. Returns ``(pattern, task, participants)`` with the
        participant schema ``_team_from_router_roster`` materializes.
        """
        client = self._responses_client(await self._bearer())
        try:
            response = await client.responses.create(
                model=self._model,
                instructions=self._instructions(),
                input=cast(Any, self._composer_input(prompt, None)),
                tools=cast(Any, [_compose_tool(["magentic"])]),
                tool_choice=cast(Any, {"type": "function", "name": "compose"}),
                store=False,
            )
        finally:
            await client.close()
        call = next(
            (
                item
                for item in getattr(response, "output", None) or []
                if getattr(item, "type", None) == "function_call"
                and getattr(item, "name", "") == "compose"
            ),
            None,
        )
        if call is None:
            raise HTTPException(
                status_code=422,
                detail="El composer no compuso un plan para esta petición",
            )
        try:
            pattern, task, participants = _read_composition(
                getattr(call, "arguments", None), ["magentic"]
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        logger.info(
            "Composer (plan): pattern=%s task=%s participants=%s workspace=%s",
            pattern,
            task[:120],
            [p.get("name") for p in participants],
            self._workspace_id or "-",
        )
        return pattern, task or prompt, participants

    async def invoke(
        self,
        prompt: str,
        history: Optional[list] = None,
        allow_plan: bool = True,
        file_ids: Optional[list[str]] = None,
        **_ignored,
    ):
        """Chat position: the composer answers, or composes and runs.

        Text streams straight to the user as it arrives. A ``compose`` call
        with ``magentic`` is left in ``composition`` (the SSE handler creates
        the Plan and ends this stream); any other pattern runs inside this turn
        through ``_run_pattern``. ``allow_plan=False`` (a turn inside a plan)
        takes ``magentic`` out of the offer: a plan never spawns another plan.
        """
        patterns = [p for p in _PATTERNS if allow_plan or p != "magentic"]
        client = self._responses_client(await self._bearer())
        composition: Optional[tuple[str, str, list[dict]]] = None
        pending = ""
        marker_blocked = False
        try:
            stream = await client.responses.create(
                model=self._model,
                instructions=self._instructions(),
                input=cast(Any, self._composer_input(prompt, history)),
                tools=cast(Any, [_compose_tool(patterns)]),
                tool_choice="auto",
                stream=True,
                store=False,
            )
            async for evt in stream:
                etype = getattr(evt, "type", None)
                if etype == "response.output_text.delta":
                    delta = getattr(evt, "delta", "") or ""
                    if not delta or marker_blocked:
                        continue
                    # A fabricated turn-log never reaches the user: the marker
                    # may arrive split across deltas, so the last len-1 chars
                    # are held back until the next delta settles them.
                    pending += delta
                    marker_idx = pending.find(_TURN_LOG_MARKER)
                    if marker_idx >= 0:
                        safe = pending[:marker_idx]
                        if safe:
                            yield self._text_update(safe)
                        marker_blocked = True
                        pending = ""
                        logger.warning(
                            "Composer answer contained %s marker; truncated.",
                            _TURN_LOG_MARKER,
                        )
                        continue
                    hold = len(_TURN_LOG_MARKER) - 1
                    if len(pending) > hold:
                        safe = pending[:-hold]
                        if safe:
                            yield self._text_update(safe)
                        pending = pending[-hold:]
                elif etype == "response.output_item.done":
                    item = getattr(evt, "item", None)
                    if (
                        getattr(item, "type", None) == "function_call"
                        and getattr(item, "name", "") == "compose"
                    ):
                        try:
                            composition = _read_composition(
                                getattr(item, "arguments", None), patterns
                            )
                        except ValueError as e:
                            logger.warning(
                                "Composer returned an unusable composition: %s", e
                            )
        finally:
            await client.close()

        if pending and not marker_blocked:
            yield self._text_update(pending)
        if composition is None:
            logger.info(
                "Composer answered directly (workspace=%s)", self._workspace_id or "-"
            )
            return

        pattern, task, participants = composition
        task = task or prompt
        logger.info(
            "Composer: pattern=%s task=%s participants=%s workspace=%s",
            pattern,
            task[:120],
            [p.get("name") for p in participants],
            self._workspace_id or "-",
        )
        self.composition = (pattern, task, participants)
        if pattern == "magentic":
            # The formal Plan leaves this turn: the SSE handler reads
            # ``composition`` after the stream and creates the Plan.
            return
        async for update in self._run_pattern(pattern, task, participants, history):
            yield update

    async def _run_pattern(
        self,
        pattern: str,
        task: str,
        participants: list[dict],
        history: Optional[list],
    ):
        """Build the composed participants with the existing factory and run
        them with the framework builder of ``pattern`` inside this turn.

        The participants receive the conversation and the task. The workflow's
        events are yielded as they are — the framework's protocol, no shims —
        and the SSE handler renders agent output, tool activity and who is
        speaking (``executor_id``). When
        a participant asks the user — Handoff hands control back after a
        response without handoff — the workflow idles and the turn ends there:
        the next message is a new composition with the conversation. The
        agents live for this turn only and are closed at its end.
        """
        from agent_framework import Message

        from v4.magentic_agents.magentic_agent_factory import MagenticAgentFactory

        if self._memory_store is None:
            raise RuntimeError("a composed orchestration needs the memory store")
        team = await _team_from_router_roster(
            participants,
            task,
            self._user_id,
            self._memory_store,
            self._workspace_id,
            with_proxy=False,
            persist=False,
        )
        agents = await MagenticAgentFactory().get_agents(
            self._user_id,
            team,
            self._memory_store,
            user_access_token=self._user_access_token,
            workspace_id=self._workspace_id,
        )
        workflow, closables = OrchestrationManager.build_pattern_workflow(
            pattern, agents
        )
        try:
            messages = [
                Message(role=str(h.get("role") or "user"), text=str(h.get("content")))
                for h in (history or [])
                if isinstance(h, dict) and h.get("content")
            ] + [Message(role="user", text=task)]
            async for event in workflow.run(messages, stream=True):
                if getattr(event, "type", None) == "request_info":
                    # Chat has no durable park/resume: the agents close with the
                    # turn and the request id would be unanswerable. The
                    # participant's question already streamed as agent output;
                    # the run ends here and the user's next message is a new
                    # composition with the conversation.
                    logger.info(
                        "Composed %s asked the user; the turn ends here", pattern
                    )
                    break
                yield event
        finally:
            for resource in closables:
                try:
                    close_method = getattr(resource, "close", None)
                    if callable(close_method):
                        result = close_method()
                        if inspect.isawaitable(result):
                            await result
                except Exception as e:
                    logger.warning(
                        "Closing workflow resource %s failed: %s",
                        getattr(resource, "name", resource),
                        e,
                    )
            for ag in agents:
                try:
                    await ag.close()
                except Exception as e:  # teardown of the turn's agents
                    logger.warning(
                        "Closing composed agent %s failed: %s",
                        getattr(ag, "agent_name", ag),
                        e,
                    )

    def _spawn_container_file_persist(
        self,
        file_id: Optional[str],
        container_id: Optional[str],
        filename: Optional[str],
    ) -> None:
        """Copy a code-interpreter output file to the persistent store the
        moment it exists — its Foundry container expires minutes later.

        Fetches with THIS client's bearer (OBO or shared): a user-scoped
        container is invisible to any other identity (same rule as
        /chat/download-file).
        """
        if not file_id or not container_id:
            return

        async def _run() -> None:
            from openai import AsyncOpenAI

            from v4.common.services.generated_file_store import GeneratedFileStore

            bearer = await self._bearer()
            client = AsyncOpenAI(
                api_key=bearer,
                base_url=self._openai_base_url,
                default_query={"api-version": _DIRECT_RESPONSES_API_VERSION},
                timeout=120,
            )
            try:
                name = filename
                if not name:
                    info: Any = await client.containers.files.retrieve(
                        file_id=file_id, container_id=container_id
                    )
                    path = getattr(info, "path", None) or file_id
                    name = os.path.basename(path)
                content = await client.containers.files.content.retrieve(
                    file_id=file_id, container_id=container_id
                )
                data = await content.aread()
            finally:
                await client.close()
            await GeneratedFileStore.get_instance().save(file_id, name or file_id, data)
            if self._workspace_id:
                from v4.common.services.workspace_service import (
                    _git,
                    _resolve,
                    workspace_for,
                )

                try:
                    _ws = workspace_for(self._user_id, self._workspace_id)
                    _dest = _resolve(_ws, name or file_id)
                    _dest.parent.mkdir(parents=True, exist_ok=True)
                    _dest.write_bytes(data)
                    _rel = str(_dest.relative_to(_ws))

                    add_res = _git(_ws, "add", _rel)
                    if add_res.returncode != 0:
                        raise RuntimeError(
                            "git add failed: "
                            + add_res.stderr.decode("utf-8", errors="replace")[-300:]
                        )

                    if _git(_ws, "diff", "--cached", "--quiet").returncode != 0:
                        commit_res = _git(
                            _ws, "commit", "-q", "-m", f"agent: add {name or file_id}"
                        )
                        if commit_res.returncode != 0:
                            raise RuntimeError(
                                "git commit failed: "
                                + commit_res.stderr.decode("utf-8", errors="replace")[
                                    -300:
                                ]
                            )
                except Exception as _ws_err:
                    logger.warning(
                        "workspace write failed for file_id=%s: %s", file_id, _ws_err
                    )

        _spawn_bg_persist(_run(), f"codeinterp:{file_id}")

    async def close(self) -> None:
        # Close only the per-user OBO credential we created here. The shared app
        # credential (used when no user token is present) is owned by the app
        # lifespan and must not be closed.
        if self._user_cred is not None:
            try:
                await self._user_cred.close()
            except Exception:
                pass
            self._user_cred = None


async def _create_direct_response_workflow(
    user_id: str,
    tenant_id: str,
    team_config_input: Optional[TeamConfiguration] = None,
    user_access_token: Optional[str] = None,
    proxy_only: bool = False,
) -> tuple[Any, list[Any], TeamConfiguration]:
    """Create a Magentic workflow for a single direct chat request.

    When ``proxy_only`` is True, only the LLM/MCP ProxyAgent is instantiated and
    the Magentic workflow graph is skipped (returned as ``None``). This is the
    SSE path, which invokes the selected agent directly (``agent.invoke``) and
    never runs the workflow — so building the other team members (and opening
    their MCP sessions) is pure latency. The full ``direct_team`` config is still
    returned so the ProxyAgent prompt keeps the whole-team context. Falls back to
    the full build if no LLM ProxyAgent entry exists (unchanged behavior)."""
    from common.config.app_config import config
    from v4.magentic_agents.magentic_agent_factory import MagenticAgentFactory
    from v4.magentic_agents.proxy_agent import ProxyAgent

    memory_store = await DatabaseFactory.get_database(
        user_id=user_id, tenant_id=tenant_id
    )
    team_service = TeamService(memory_store)
    if team_config_input is not None:
        direct_team = team_config_input
        if not getattr(direct_team, "deployment_name", None):
            direct_team.deployment_name = config.AZURE_OPENAI_DEPLOYMENT_NAME
    else:
        teams = await team_service.get_all_team_configurations()
        if not teams:
            current = await memory_store.get_current_team(user_id=user_id)
            if current:
                team = await memory_store.get_team_by_id(team_id=current.team_id)
                if team:
                    teams = [team]
        if not teams:
            raise ValueError("No teams configured for direct response")

        current = await memory_store.get_current_team(user_id=user_id)
        current_team = (
            await memory_store.get_team_by_id(team_id=current.team_id)
            if current
            else None
        )
        default_deployment_name = (
            getattr(current_team, "deployment_name", None)
            or getattr(teams[0], "deployment_name", None)
            or config.AZURE_OPENAI_DEPLOYMENT_NAME
        )
        direct_team = _merge_teams_for_direct_response(
            teams,
            user_id=user_id,
            default_deployment_name=default_deployment_name,
        )

    # Direct SSE path invokes a single agent (always the ProxyAgent) directly, so
    # build only that agent and skip the unused Magentic workflow graph.
    build_team = direct_team
    if proxy_only:
        proxy_entry = next(
            (
                a
                for a in getattr(direct_team, "agents", []) or []
                if (getattr(a, "name", "") or "").lower() == "proxyagent"
                and getattr(a, "deployment_name", "")
            ),
            None,
        )
        if proxy_entry is not None:
            build_team = direct_team.model_copy(update={"agents": [proxy_entry]})
        else:
            logger.warning(
                "proxy_only requested but no LLM ProxyAgent entry found; "
                "building full direct-response team."
            )

    agents = await MagenticAgentFactory(team_service=team_service).get_agents(
        user_id=user_id,
        team_config_input=build_team,
        memory_store=memory_store,
        user_access_token=user_access_token,
    )
    agents = [agent for agent in agents if _agent_can_join_direct_orchestration(agent)]
    if not agents:
        raise ValueError("No executable agents available for direct response")

    for agent in agents:
        if isinstance(agent, ProxyAgent):
            agent.session_id = ""

    if build_team is not direct_team:
        # ProxyAgent-only build: the workflow graph is never run in this path.
        return None, agents, direct_team

    # Full build fallback (proxy_only=False or no ProxyAgent entry found).
    # This path is not used by the SSE chat stream but kept for future callers.
    raise NotImplementedError(
        "Full direct-response workflow build is no longer supported. "
        "Use proxy_only=True or route TASK intent through process_request."
    )


async def _close_direct_response_agents(agents: list[Any]) -> None:
    for agent in agents:
        close_method = getattr(agent, "close", None)
        if callable(close_method):
            try:
                result = close_method()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning(
                    "Could not close direct-response agent '%s': %s",
                    _agent_runtime_name(agent) or type(agent).__name__,
                    exc,
                )


@app_v4.post(
    "/chat/message/stream",
    # The route returns SSE, but FastAPI infers application/json from the
    # handler, so the published contract described a body this route never
    # sends — a generated client would parse the stream as JSON and fail.
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def chat_message_stream(
    background_tasks: BackgroundTasks,
    chat_request: ChatMessageRequest,
    request: Request,
):
    """
    Stream a chat response via Server-Sent Events (SSE).

    Same intent classification as /chat/message, but streams LLM tokens
    in real-time instead of returning a single JSON response.

    SSE event types:
    - {type: "intent", intent, confidence, session_id}
    - {type: "token", content}       — streamed LLM token
    - {type: "redirect", redirect_to_plan, session_id} — task intent
    - {type: "done", intent, agent, confidence, session_id}
    - {type: "error", message}
    """

    from v4.orchestration.intent_router import Intent

    try:
        authenticated_user = get_authenticated_user_details(
            request_headers=request.headers
        )
    except PermissionError as exc:
        # Auth failure must surface as 401 — never an unhandled 500. The frontend
        # SSE client (apiClient.stream) only refresh-retries on 401; a 500 skips
        # the retry, kills the chat, and forces a full page reload. Same pattern
        # workspace_router already uses for its endpoints.
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user_id = authenticated_user["user_principal_id"]
    # Identidad del turno (la acuña el cliente): permite abortarlo por identidad.
    _turn_key = (user_id, chat_request.turn_id) if chat_request.turn_id else None

    def _turn_aborted() -> bool:
        return _turn_key is not None and _ACTIVE_TURNS.get(_turn_key, False)

    tenant_id = authenticated_user.get("tenant_id", "")
    # End-user token for on-behalf-of invocation of the hosted agent, so its
    # Toolbox sees a real delegated user context (not the app Managed Identity).
    user_access_token = authenticated_user.get("access_token")

    if not chat_request.session_id:
        chat_request.session_id = str(uuid.uuid4())

    # ── Pre-stream work: persist user message + classify intent ──
    chat_svc = await get_chat_cosmos_service()
    memory_store = await DatabaseFactory.get_database(
        user_id=user_id, tenant_id=tenant_id
    )
    active_plan = None
    active_plan_team = None
    active_m_plan_id: Optional[str] = None
    if chat_request.plan_id:
        active_plan = await memory_store.get_plan_by_plan_id(
            plan_id=chat_request.plan_id
        )
        if not active_plan:
            raise HTTPException(
                status_code=404,
                detail=f"Plan '{chat_request.plan_id}' not found",
            )
        active_m_plan_id = _get_m_plan_id_from_plan(active_plan)
        plan_team_id = getattr(active_plan, "team_id", None)
        if plan_team_id:
            active_plan_team = await memory_store.get_team_by_id(team_id=plan_team_id)
        if not active_plan_team:
            raise HTTPException(
                status_code=404,
                detail=f"Team for plan '{chat_request.plan_id}' not found",
            )
        if getattr(active_plan, "session_id", None):
            chat_request.session_id = active_plan.session_id

    async def _persist_user_message() -> None:
        # Se persiste al CERRAR el turno (junto a la respuesta), no al abrirlo:
        # un turno abortado por el cliente no deja rastro, igual que en la UI.
        try:
            await chat_svc.add_message(
                session_id=chat_request.session_id,
                user_id=user_id,
                content=chat_request.message,
                role="user",
                metadata={
                    "plan_id": chat_request.plan_id,
                    "m_plan_id": active_m_plan_id,
                }
                if chat_request.plan_id
                else None,
            )
        except Exception as e:
            logger.warning("Could not persist user chat message: %s", e)

    previous_intent = await _get_previous_intent(
        chat_svc, chat_request.session_id, user_id
    )

    # Una respuesta a clarificación lleva la identidad de su pregunta
    # (clarification_request_id). Sin ella este mensaje es una tarea nueva,
    # aunque la sesión tenga un plan aparcado. Decidir por sesión que "este
    # mensaje es la respuesta" secuestraba tareas nuevas como respuestas a
    # preguntas que el usuario nunca vio: prod 2026-09-22, autonoma-001, plan
    # e5b31dda aparcado desde el día anterior, dos tareas tragadas como
    # respuestas a c817f2a3 y df7940b6, y el run acabó en 400.
    pending_plan = await _clarification_answer_target(memory_store, chat_request)
    if pending_plan is not None:
        pending_request_id = (pending_plan.waiting_for or {})["request_id"]
        logger.info(
            "Routing message as clarification answer for request_id=%s session=%s",
            pending_request_id,
            chat_request.session_id,
        )
        await _persist_user_message()
        # The answer resumes the parked workflow from its checkpoint (no in-process wait).
        await _append_event(
            kind="clarification",
            request_id=pending_request_id,
            user_id=user_id,
            tenant_id=tenant_id,
            user_access_token=user_access_token,
            payload={"answer": chat_request.message},
        )
        # Persist the exchange to the single chat history.
        try:
            await chat_svc.add_message(
                session_id=chat_request.session_id,
                user_id=user_id,
                content="✅ Clarification submitted. Processing…",
                role="assistant",
                metadata={"intent": "task"},
            )
        except Exception as _e:
            logger.warning("Could not persist clarification ack: %s", _e)

        async def _clarification_stream():
            yield _sse_event(
                {
                    "type": "intent",
                    "intent": "task",
                    "confidence": 1.0,
                    "session_id": chat_request.session_id,
                    "plan_id": chat_request.plan_id,
                    "m_plan_id": active_m_plan_id,
                }
            )
            yield _sse_event(
                {
                    "type": "token",
                    "content": "✅ Clarification submitted. Processing…",
                }
            )
            yield _sse_event(
                {
                    "type": "done",
                    "intent": "task",
                    "agent": "assistant",
                    "confidence": 1.0,
                    "session_id": chat_request.session_id,
                    "plan_id": chat_request.plan_id,
                    "m_plan_id": active_m_plan_id,
                }
            )

        return StreamingResponse(
            _clarification_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    # ── End clarification guard ──────────────────────────────────

    # Intent router removed from chat: the deployed Hosted Agent orchestrator
    # answers ALL chat and decides tool use / clarification itself (ReAct loop).
    # Formal Plan mode is reached explicitly (future UI selector → process_request),
    # not inferred here — this collapses the old router → intent-router → router
    # double hop. The pending-clarification guard above still short-circuits when a
    # plan is actively waiting for an answer.
    from v4.orchestration.intent_router import IntentResult

    intent_result = IntentResult(
        intent=Intent.CONVERSATIONAL,
        confidence=1.0,
        reasoning="hosted orchestrator answers all chat",
    )
    logger.info(
        "Chat stream (hosted orchestrator, no intent routing; prev=%s): %s",
        previous_intent,
        chat_request.message[:80],
    )
    plan_id: Optional[str] = None

    # ── SSE async generator ──────────────────────────────────────
    async def event_stream():
        _aborted_flag = False
        if _turn_key:
            _ACTIVE_TURNS[_turn_key] = False
        # 1. Intent event
        yield _sse_event(
            {
                "type": "intent",
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "session_id": chat_request.session_id,
                "plan_id": chat_request.plan_id or plan_id,
                "m_plan_id": active_m_plan_id,
            }
        )

        if intent_result.intent == Intent.TASK:
            redirect_msg = (
                "I've created a plan for your request. Redirecting to plan view."
            )
            # process_request already wrote the task anchor to chat_cosmos.
            if plan_id:
                yield _sse_event({"type": "token", "content": redirect_msg})
                yield _sse_event(
                    {
                        "type": "plan_created",
                        "plan_id": plan_id,
                        "session_id": chat_request.session_id,
                    }
                )
            else:
                yield _sse_event(
                    {
                        "type": "token",
                        "content": "Sorry, I couldn't create a plan. Please try again.",
                    }
                )
            yield _sse_event(
                {
                    "type": "done",
                    "intent": "task",
                    "agent": "planner",
                    "confidence": intent_result.confidence,
                    "session_id": chat_request.session_id,
                    "plan_id": plan_id,
                }
            )
            return

        full_text = ""
        collected_generated_files: list[dict] = []
        _cleanup = AsyncExitStack()
        last_mcp_tool_call: Optional[tuple[str, str]] = None
        current_speaker: Optional[str] = None
        # A function_result carries only its call_id; the name comes from
        # the function_call that opened it.
        _call_names: dict[str, str] = {}

        try:
            await _cleanup.__aenter__()
            code_interpreter_call_emitted = False

            from common.config.app_config import config

            orchestrator_name = (
                getattr(config, "CHAT_ORCHESTRATOR_AGENT_NAME", "") or ""
            ).strip()
            if not orchestrator_name:
                raise ValueError(
                    "CHAT_ORCHESTRATOR_AGENT_NAME is not set; no chat agent configured."
                )
            agent = _RouterChatClient(
                orchestrator_name,
                user_access_token=user_access_token,
                user_id=user_id,
                workspace_id=chat_request.workspace_id,
                memory_store=memory_store,
            )
            _cleanup.push_async_callback(agent.close)
            selected_agent_name = orchestrator_name
            direct_team_name = "Hosted Orchestrator"
            foundry_agents: list = []
            # The Hosted Agent has its own system prompt — send the message raw.
            direct_chat_prompt = chat_request.message

            # --- Ensure MCP OAuth before invoking the agent ---
            try:
                mcp_cfg = getattr(agent, "mcp_cfg", None)
                if mcp_cfg and getattr(mcp_cfg, "name", None):
                    import os

                    from v4.api.oauth_helpers import build_authorize_url, sign_state
                    from v4.common.models.mcp_connection_models import (
                        MCPAuthType,
                        MCPConnectionStatus,
                    )
                    from v4.common.services.mcp_connections_service import (
                        MCPConnectionsService,
                    )

                    svc = await MCPConnectionsService.get_instance()
                    server = await svc.get_server_by_name(
                        mcp_cfg.name,
                        tenant_id=tenant_id,
                    )
                    if server and server.auth_type == MCPAuthType.OAUTH2:
                        user_conn = await svc.get_user_connection(
                            user_id, server.server_name, tenant_id=tenant_id
                        )
                        if (
                            not user_conn
                            or user_conn.status != MCPConnectionStatus.ACTIVE
                        ):
                            # Build consent link if possible
                            client_id_env = server.oauth_client_id_env or ""
                            client_id = (
                                os.environ.get(client_id_env, "")
                                if client_id_env
                                else ""
                            )
                            consent_link = None
                            if server.oauth_authorize_url and client_id:
                                state = sign_state(user_id, server.server_name)
                                consent_link = build_authorize_url(
                                    server.oauth_authorize_url,
                                    client_id,
                                    server.oauth_scopes or [],
                                    state,
                                )
                            else:
                                consent_link = server.oauth_authorize_url or ""
                            logger.info(
                                "Pre-invoke: user needs OAuth for MCP server: %s",
                                server.server_name,
                            )
                            yield _sse_event(
                                {
                                    "type": "oauth_consent_request",
                                    "consent_link": consent_link,
                                    "message": "Authorization required to use this MCP server",
                                }
                            )
                            return
            except Exception as _pre_check_exc:
                logger.warning("MCP auth pre-check failed: %s", _pre_check_exc)
            # --- end pre-check ---

            _last_tool_activity_key: Optional[tuple] = None
            # Turn ledger: the "floating membranes" (tool calls + args + result
            # heads) that used to evaporate with store=False. Persisted in
            # assistant metadata.turn_log at close (NOT in content) so the deeds
            # of this turn are auditable without contaminating router memory.
            _turn_ledger: list = []
            _turn_ledger_dropped: int = 0
            _ledger_pending_args: str = ""

            # Rebuild conversation memory from the REAL plumbing (Cosmos + Azure AI
            # Search), NOT previous_response_id (fragile: breaks on re-auth / session
            # regen). Two layers, deduped, oldest→newest:
            #   long memory  → hybrid keyword+vector+semantic retrieval across ALL of
            #                  the user's history (search_chat_history), so a relevant
            #                  fact from any past session/turn comes back regardless
            #                  of any sliding window;
            #   short memory → this session's turns in order (authoritative
            #                  conversational continuity).
            # add_message already writes+indexes every turn, so this read closes the
            # loop. The current user message was just persisted, so it is skipped.
            _history = await _recover_session_context(
                chat_svc, chat_request.session_id, user_id, chat_request.message
            )
            logger.info(
                "Recovered %d context messages (Cosmos+Search) for session=%s",
                len(_history),
                chat_request.session_id[:12],
            )

            _invoke_kwargs: dict = {
                "session_id": chat_request.session_id,
                "user_id": user_id,
                "file_ids": chat_request.file_ids,
                "history": _history,
                # In-plan turns never create a NEW plan regardless of the flag.
                "allow_plan": chat_request.allow_plan and not chat_request.plan_id,
            }

            async for update in agent.invoke(
                direct_chat_prompt,
                **_invoke_kwargs,
            ):
                if _turn_aborted():
                    logger.info(
                        "Chat turn %s aborted by client: stopping generation",
                        chat_request.turn_id,
                    )
                    break
                # Framework protocol, no shims: the composer's own words arrive
                # as an AgentResponseUpdate; a composed pattern's run arrives as
                # the workflow's WorkflowEvents, of which only agent output
                # carries contents. The speaking participant is the executor.
                if isinstance(update, WorkflowEvent):
                    # request_info never reaches this stream: the chat lane has
                    # no park/resume, so _run_pattern ends the composed run
                    # there instead of forwarding an unanswerable request.
                    if update.type != "output":
                        continue
                    _data = update.data
                    speaker = update.executor_id
                    if isinstance(_data, AgentResponseUpdate):
                        contents = list(_data.contents or [])
                    elif isinstance(_data, AgentResponse):
                        contents = [
                            c
                            for m in (_data.messages or [])
                            for c in (m.contents or [])
                        ]
                    else:
                        continue
                else:
                    contents = list(getattr(update, "contents", None) or [])
                    speaker = getattr(update, "author_name", None)
                if speaker and speaker != current_speaker:
                    current_speaker = speaker
                    yield _sse_event({"type": "agent", "agent": speaker})
                for content in contents:
                    ct = content.type
                    content_preview = (
                        getattr(content, "text", None)
                        or getattr(content, "message", None)
                        or getattr(content, "input", None)
                        or getattr(content, "output", None)
                        or getattr(content, "stderr", None)
                        or getattr(content, "stdout", None)
                        or ""
                    )
                    logger.info(
                        "SSE content type=%s, name=%s, server=%s, text=%s",
                        ct,
                        getattr(content, "name", None)
                        or getattr(content, "tool_name", None)
                        or "",
                        getattr(content, "server_name", None) or "",
                        str(content_preview)[:200],
                    )

                    if ct == "text":
                        token = content.text or ""
                        if token:
                            full_text += token
                            yield _sse_event(
                                {
                                    "type": "token",
                                    "content": token,
                                    "agent": current_speaker,
                                }
                            )

                    elif ct == "function_call":
                        logger.info(
                            "Function call: name=%s args=%s",
                            content.name,
                            content.arguments,
                        )
                        _call_id = getattr(content, "call_id", None)
                        if _call_id and content.name:
                            _call_names[str(_call_id)] = content.name
                        _key = ("calling", content.name or "unknown")
                        if _key != _last_tool_activity_key:
                            _last_tool_activity_key = _key
                            yield _sse_event(
                                {
                                    "type": "tool_activity",
                                    "activity": "calling",
                                    "tool": content.name or "unknown",
                                    "args": str(content.arguments or "")[:200],
                                }
                            )

                    elif ct == "function_result":
                        _tool_name = getattr(content, "name", None) or _call_names.get(
                            str(getattr(content, "call_id", None) or ""), None
                        )
                        _result_obj = getattr(content, "result", content)
                        _result_preview = _safe_json_dumps(_to_safe_dict(_result_obj))[
                            :1000
                        ]
                        logger.info(
                            "Function result: name=%s result=%s",
                            getattr(content, "name", "?"),
                            _result_preview[:4000],
                        )
                        _key = ("result", _tool_name or "unknown")
                        if _key != _last_tool_activity_key:
                            _last_tool_activity_key = _key
                            yield _sse_event(
                                {
                                    "type": "tool_activity",
                                    "activity": "result",
                                    "tool": _tool_name or "unknown",
                                    "success": content.exception is None,
                                    "result_preview": _result_preview,
                                }
                            )

                    elif ct == "mcp_server_tool_call":
                        tool_name = getattr(content, "tool_name", None) or "unknown"
                        server_name = getattr(content, "server_name", None) or "unknown"
                        last_mcp_tool_call = (tool_name, server_name)
                        # Completo: la cota (con flag) la pone _make_deed.
                        _ledger_pending_args = str(content.arguments or "")
                        # La UI y la voz narran la tool REAL (dentro de los
                        # argumentos del envoltorio), no "call external tool".
                        _tool_lbl, _server_lbl = describe_tool_call(
                            tool_name, server_name, content.arguments
                        )
                        _mcp_call_key = ("calling", _tool_lbl, _server_lbl)
                        if _mcp_call_key != _last_tool_activity_key:
                            _last_tool_activity_key = _mcp_call_key
                            yield _sse_event(
                                {
                                    "type": "tool_activity",
                                    "activity": "calling",
                                    "tool": _tool_lbl,
                                    "server": _server_lbl,
                                    "wrapper": tool_name,
                                    "args": str(content.arguments or "")[:200],
                                }
                            )

                    elif ct == "mcp_server_tool_result":
                        tool_name = getattr(content, "tool_name", None)
                        server_name = getattr(content, "server_name", None)
                        if last_mcp_tool_call:
                            tool_name = tool_name or last_mcp_tool_call[0]
                            server_name = server_name or last_mcp_tool_call[1]
                        tool_name = tool_name or "unknown"
                        server_name = server_name or "unknown"
                        last_mcp_tool_call = None
                        if len(_turn_ledger) < _LEDGER_MAX_DEEDS:
                            _turn_ledger.append(
                                _make_deed(
                                    server_name,
                                    tool_name,
                                    _ledger_pending_args,
                                    "error"
                                    if getattr(content, "status", None) == "error"
                                    else "success",
                                    content_preview,
                                )
                            )
                        else:
                            _turn_ledger_dropped += 1
                        _ledger_pending_args = ""
                        _mcp_result_key = ("result", tool_name, server_name)
                        if _mcp_result_key != _last_tool_activity_key:
                            _last_tool_activity_key = _mcp_result_key
                            yield _sse_event(
                                {
                                    "type": "tool_activity",
                                    "activity": "result",
                                    "tool": tool_name,
                                    "server": server_name,
                                    "success": content.status != "error"
                                    if content.status
                                    else True,
                                }
                            )
                        # ca-mcp connect_from_registry -> pending_auth with an
                        # oauth_url (discovery lane). Reuse the SAME popup the
                        # composers already wire for oauth_consent_request; on
                        # close they re-send the message and the retry connects
                        # with the fresh token.
                        _oauth_m = re.search(
                            r"['\"]oauth_url['\"]\s*:\s*['\"]([^'\"]+)['\"]",
                            str(content_preview or ""),
                        )
                        if _oauth_m:
                            _oauth_link = _oauth_m.group(1).replace("\\/", "/")
                            logger.info(
                                "OAuth consent required (discovered) for %s (consent link generated)",
                                server_name,
                            )
                            yield _sse_event(
                                {
                                    "type": "oauth_consent_request",
                                    "consent_link": _oauth_link,
                                    "message": "Authorization required to use this MCP server",
                                }
                            )

                    elif ct == "code_interpreter_tool_call":
                        args_text = str(
                            getattr(content, "input", None)
                            or getattr(content, "text", None)
                            or getattr(content, "arguments", None)
                            or ""
                        )[:500]
                        # Foundry may emit many empty deltas for the same run.
                        # Keep the real signal: one generic call if empty, or
                        # the first payload-bearing call if present.
                        if not code_interpreter_call_emitted:
                            yield _sse_event(
                                {
                                    "type": "tool_activity",
                                    "activity": "calling",
                                    "tool": "code_interpreter",
                                    "args": args_text,
                                }
                            )
                            code_interpreter_call_emitted = True

                    elif ct == "code_interpreter_tool_result":
                        result_text = (
                            getattr(content, "stderr", None)
                            or getattr(content, "output", None)
                            or getattr(content, "stdout", None)
                            or getattr(content, "text", None)
                            or getattr(content, "message", None)
                            or ""
                        )
                        yield _sse_event(
                            {
                                "type": "tool_activity",
                                "activity": "result",
                                "tool": "code_interpreter",
                                "success": not bool(getattr(content, "stderr", None)),
                                "message": str(result_text)[:500],
                            }
                        )
                        code_interpreter_call_emitted = False

                        for ann in getattr(content, "annotations", None) or []:
                            ann_dict = ann if isinstance(ann, dict) else {}
                            fid = ann_dict.get("file_id") or getattr(
                                ann, "file_id", None
                            )
                            add_props = ann_dict.get(
                                "additional_properties"
                            ) or getattr(ann, "additional_properties", None)
                            container_id = (
                                add_props.get("container_id")
                                if isinstance(add_props, dict)
                                else None
                            )
                            fname = (
                                ann_dict.get("url")
                                or (
                                    add_props.get("filename")
                                    if isinstance(add_props, dict)
                                    else None
                                )
                                or ann_dict.get("text")
                                or fid
                                or "generated_file"
                            )
                            if fid:
                                logger.info(
                                    "code_interpreter generated file: file_id=%s name=%s",
                                    fid,
                                    fname,
                                )
                                _gf_entry = {
                                    "type": "generated_file",
                                    "file_id": fid,
                                    "filename": fname,
                                    "container_id": container_id,
                                    "download_url": (
                                        f"/api/v4/chat/download-file/{fid}?container_id={container_id}"
                                        if container_id
                                        else f"/api/v4/chat/download-file/{fid}"
                                    ),
                                }
                                collected_generated_files.append(_gf_entry)
                                yield _sse_event(_gf_entry)
                                # Express the file IN the message content —
                                # markdown image (rendered inline in the
                                # bubble) or link — so it persists and
                                # re-renders with the conversation.
                                _md = (
                                    f"\n\n![{fname}]({_gf_entry['download_url']})"
                                    if str(fname)
                                    .lower()
                                    .endswith(
                                        (".png", ".jpg", ".jpeg", ".webp", ".gif")
                                    )
                                    else f"\n\n[{fname}]({_gf_entry['download_url']})"
                                )
                                full_text += _md
                                yield _sse_event({"type": "token", "content": _md})

                    elif ct == "hosted_file":
                        # Streaming path: agent_framework emits container_file_citation
                        # as a hosted_file Content with metadata in additional_properties
                        # (verified in agent_framework/openai/_responses_client.py:2157).
                        fid = getattr(content, "file_id", None)
                        add_props = (
                            getattr(content, "additional_properties", None) or {}
                        )
                        container_id = (
                            add_props.get("container_id")
                            if isinstance(add_props, dict)
                            else None
                        )
                        fname = (
                            (
                                add_props.get("filename")
                                if isinstance(add_props, dict)
                                else None
                            )
                            or getattr(content, "name", None)
                            or fid
                            or "generated_file"
                        )
                        if fid:
                            logger.info(
                                "hosted_file content: file_id=%s container_id=%s name=%s",
                                fid,
                                container_id,
                                fname,
                            )
                            _gf_entry = {
                                "type": "generated_file",
                                "file_id": fid,
                                "filename": fname,
                                "container_id": container_id,
                                "download_url": (
                                    f"/api/v4/chat/download-file/{fid}?container_id={container_id}"
                                    if container_id
                                    else f"/api/v4/chat/download-file/{fid}"
                                ),
                            }
                            collected_generated_files.append(_gf_entry)
                            yield _sse_event(_gf_entry)
                            # Same unified rule: the file IS message content.
                            _md = (
                                f"\n\n![{fname}]({_gf_entry['download_url']})"
                                if str(fname)
                                .lower()
                                .endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
                                else f"\n\n[{fname}]({_gf_entry['download_url']})"
                            )
                            full_text += _md
                            yield _sse_event({"type": "token", "content": _md})
                            # Persist to Blob (GeneratedFileStore) and workspace in parallel.
                            # Fire-and-forget: container file expires minutes after creation.
                            agent._spawn_container_file_persist(
                                fid, container_id, fname
                            )

                    elif ct == "oauth_consent_request":
                        # Tool (e.g. GitHub MCP) needs the user to complete OAuth.
                        # Emit the consent link so the frontend can open a popup.
                        consent_link = getattr(content, "consent_link", None)
                        if consent_link:
                            logger.info("OAuth consent required: %s", consent_link)
                            yield _sse_event(
                                {
                                    "type": "oauth_consent_request",
                                    "consent_link": consent_link,
                                }
                            )

                    elif ct == "function_approval_request":
                        # Agent requires user approval before executing a tool call.
                        # Emit an SSE event so the frontend can render the approval UI.
                        fc = getattr(content, "function_call", None)
                        yield _sse_event(
                            {
                                "type": "function_approval_request",
                                "approval_id": getattr(content, "id", None),
                                "tool": getattr(fc, "name", None) or "unknown",
                                "args": str(getattr(fc, "arguments", "") or "")[:500],
                            }
                        )

                    elif ct == "text_reasoning":
                        # Agent's internal reasoning — send as thinking indicator
                        yield _sse_event(
                            {
                                "type": "tool_activity",
                                "activity": "thinking",
                                "tool": "reasoning",
                            }
                        )

                    # usage, hosted_file, etc. — skip silently

            composition = getattr(agent, "composition", None)
            if composition and composition[0] == "magentic":
                # The composer chose the formal Plan. Create it + kick off the
                # orchestration (BackgroundTask + WebSocket + PlanPage), tell the
                # frontend to navigate, and end the SSE stream. The `finally`
                # above still runs cleanup; the conversational persist/done below
                # is intentionally skipped (the task anchor is written by the
                # plan creation).
                _pattern, _task, _participants = composition
                logger.info(
                    "Composer chose the formal Plan for session=%s",
                    chat_request.session_id,
                )
                try:
                    _plan_id = await _create_plan_and_start(
                        background_tasks=background_tasks,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        user_access_token=user_access_token,
                        description=_task or chat_request.message,
                        session_id=chat_request.session_id,
                        # Same context the composer already had this turn —
                        # cross the boundary instead of dropping it.
                        history=_history,
                        # Participants composed in the same call; an empty
                        # roster falls back to the user's selected team.
                        composed_agents=_participants or None,
                        workspace_id=chat_request.workspace_id,
                    )
                    yield _sse_event(
                        {
                            "type": "plan_created",
                            "plan_id": _plan_id,
                            "session_id": chat_request.session_id,
                        }
                    )
                    yield _sse_event(
                        {
                            "type": "done",
                            "intent": "task",
                            "agent": "planner",
                            "confidence": 1.0,
                            "session_id": chat_request.session_id,
                            "plan_id": _plan_id,
                        }
                    )
                except Exception as _plan_err:
                    logger.exception("Plan creation failed: %s", _plan_err)
                    yield _sse_event(
                        {
                            "type": "token",
                            "content": "Sorry, I couldn't create a plan. Please try again.",
                        }
                    )
                    yield _sse_event(
                        {
                            "type": "done",
                            "intent": "task",
                            "agent": "planner",
                            "confidence": 1.0,
                            "session_id": chat_request.session_id,
                        }
                    )
                return
        except Exception as e:
            tool_err = classify_tool_error(e)
            emit_tool_error("chat_message_stream", tool_err)
            logger.error(
                "FoundryAgent streaming failed - Category: %s, Status: %s, AADSTS: %s",
                tool_err.category.value,
                tool_err.status_code,
                tool_err.aadsts,
                exc_info=True,
            )
            if not full_text:
                if tool_err.category == ToolErrorCategory.AUTH_CONSENT:
                    yield _sse_event(
                        {
                            "type": "oauth_consent_request",
                            "message": user_message_for(tool_err),
                            "aadsts": tool_err.aadsts,
                        }
                    )
                elif tool_err.category == ToolErrorCategory.PERMISSION:
                    yield _sse_event(
                        {
                            "type": "permission_required",
                            "message": user_message_for(tool_err),
                            "suggested_action": _suggest_action_for_error(tool_err),
                        }
                    )
                else:
                    yield _sse_event(
                        {"type": "token", "content": user_message_for(tool_err)}
                    )
            else:
                yield _sse_event(
                    {
                        **_build_error_response(tool_err, chat_request.message),
                        "type": "error",
                    }
                )
        finally:
            await _cleanup.aclose()
            if _turn_key:
                _aborted_flag = _ACTIVE_TURNS.pop(_turn_key, False)

        # 4. Persist the turn (user + assistant) to Cosmos — unless the client
        # aborted it: then NOTHING of this turn is persisted, matching the UI.
        if _aborted_flag:
            logger.info(
                "Chat turn %s aborted by client: nothing persisted",
                chat_request.turn_id,
            )
        else:
            try:
                await _persist_user_message()
                _persist_meta: dict = {
                    "intent": intent_result.intent.value,
                    "selected_agent": selected_agent_name,
                    "merged_team": direct_team_name,
                }
                if active_plan:
                    _persist_meta["plan_id"] = chat_request.plan_id
                    _persist_meta["m_plan_id"] = active_m_plan_id
                    _persist_meta["team_id"] = getattr(active_plan, "team_id", None)
                if collected_generated_files:
                    # Strip internal 'type' key — only store the file descriptors
                    _persist_meta["generated_files"] = [
                        {
                            "file_id": gf["file_id"],
                            "filename": gf["filename"],
                            "container_id": gf.get("container_id"),
                            "download_url": gf["download_url"],
                        }
                        for gf in collected_generated_files
                    ]
                if _turn_ledger:
                    _persist_meta["turn_log"] = list(_turn_ledger)
                if _turn_ledger_dropped:
                    _persist_meta["turn_log_dropped"] = _turn_ledger_dropped
                _persist_content = _strip_turn_log_block(full_text)
                if _persist_content != full_text:
                    logger.warning(
                        "Sanitized assistant content containing %s before persistence.",
                        _TURN_LOG_MARKER,
                    )
                await chat_svc.add_message(
                    session_id=chat_request.session_id,
                    user_id=user_id,
                    content=_persist_content,
                    role="assistant",
                    metadata=_persist_meta,
                )
            except Exception as e:
                logger.warning("Could not persist streamed response: %s", e)

        # No conversation_id to persist: memory rides on Cosmos + AI Search, which
        # add_message already wrote+indexed above (user + assistant turns). The next
        # turn recovers context from that real plumbing — no previous_response_id.

        track_event_if_configured(
            "Chat_MultiTeam_Streaming",
            {
                "session_id": chat_request.session_id,
                "user_id": user_id,
                "intent": intent_result.intent.value,
                "response_length": len(full_text),
                "selected_agent": selected_agent_name,
                "merged_team": direct_team_name,
                "available_agents": len(foundry_agents) if foundry_agents else 0,
                "plan_id": chat_request.plan_id,
                "m_plan_id": active_m_plan_id,
            },
        )

        # 5. Done event with final metadata
        yield _sse_event(
            {
                "type": "done",
                "intent": intent_result.intent.value,
                "agent": selected_agent_name,
                "confidence": intent_result.confidence,
                "session_id": chat_request.session_id,
                "plan_id": chat_request.plan_id,
                "m_plan_id": active_m_plan_id,
            }
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Direct response fallback instructions (legacy MCP prompt text) ────────

_MCP_AGENT_INSTRUCTIONS = (
    "Eres ProxyAgent del sistema MACAE en un entorno multi-agente para sistemas empresariales críticos (ej. Work IQ Email, SharePoint, etc.). Tu misión es ejecutar las siguientes acciones de manera autónoma bajo las reglas obligatorias:\n\n"
    "1. No inventes sesiones, servidores, registry ni tools.\n"
    '2. No digas "consulté", "validé", "intenté" o "ejecuté" si no existe resultado real de tool. Nunca afirmes que ejecutaste una acción si no recibiste resultado real de una tool. Si una tool requerida no fue llamada o falló, responde: "No ejecutado" + causa exacta + siguiente comando/tool necesaria.\n'
    "3. Si el usuario pide listar servidores/tools:\n"
    "   - Primero llama la tool real disponible.\n"
    '   - Si no hay resultado, di "No disponible".\n'
    "4. Si el usuario pide filesystem:\n"
    "   - Primero conecta filesystem por stdio.\n"
    "   - Luego descubre tools.\n"
    "   - Luego ejecuta list_directory/read_file según aplique.\n"
    "5. Si falta conexión, no preguntes de nuevo si el usuario ya dio la orden. Ejecuta conexión.\n"
    "6. Respuesta técnica mínima por fase ejecutada:\n"
    "   - Acción real ejecutada\n"
    "   - Resultado real\n"
    "   - Error exacto si existe\n"
    "   - Próximo paso ejecutable\n\n"
    "CUENTAS CON ESTOS SERVIDORES REMOTOS Y SUS TOOLS:\n"
    "- Github Copilot MCP\n"
    "- Work IQ teams\n"
    "- Microsoft Learn\n"
    "- Foundry MCP Server\n"
    "- Azure Resource Manager\n"
    "- Azure DevOps MCP Server\n"
    "- SharePoint\n"
    "- Herramientas MCP nativas y expuestas del espacio mcp_MacaeMcpServer: employee_onboarding_blueprint_flat (flujo onboarding) schedule_orientation_session assign_mentor register_for_benefits provide_employee_handbook initiate_background_check request_id_card set_up_payroll send_welcome_email set_up_office_365_account configure_laptop setup_vpn_access create_system_accounts generate_press_release handle_influencer_collaboration get_product_info compare_products greet_test get_server_status data_provider show_tables.\n\n"
    "OTRAS DIRECTRICES:\n"
    "- Si una integración falla, intenta rutas alternativas automáticamente y reporta fallo tras agotar opciones.\n"
    "- NUNCA SIMULES ACCIONES, EJECUCIONES NI PROCESOS.\n"
    "- Reporta siempre con exactitud y autonomía.\n"
    "- Toda la interacción es proactiva y orientada a la resolución integral del flujo.\n"
    "- No requieras nunca pasos manuales ni intervenciones del usuario a menos que estrictamente no puedas realizarlas tú.\n"
    "- Ejecuta todas las validaciones técnicas, pruebas de conexión, detección de errores y análisis de integración de forma secuencial y autónoma.\n"
    "- Reporta resultados de cada fase con transparencia y precisión, usando lenguaje técnico, directo y sin ambigüedades.\n"
    "- El reporte debe incluir: acción realizada, resultado, errores encontrados, y pasos correctivos ejecutados automáticamente.\n"
    "- Registra y documenta cada validación, error, integración y resultado en la memoria persistente del sistema.\n"
    "- Detalla suficiente contexto para análisis posteriores y reutilización.\n"
    "- Aplica una metodología estricta, transparente y secuencial: inspección de código, validación de directorios, mapeo recursivo, logging detallado. Realiza cada fase antes de iniciar la siguiente.\n"
    "- No emitas recomendaciones ni reportes intermedios hasta completar el segmento correspondiente.\n"
    "- No repitas preguntas ni solicites confirmaciones innecesarias.\n"
    "- Solo procede y reporta avances, hallazgos, errores o bloqueos.\n"
    "- Adapta el nivel de detalle del reporte según el contexto técnico y la fase, asegurando siempre claridad, exhaustividad cuando se requiera y acción directa.\n"
    "- Prioriza la experiencia del usuario frontend asegurando operaciones fluidas y sin fricción.\n\n"
    "IMPORTANTE: Está estrictamente prohibido activar patrones de confusión, especialmente al realizar validaciones usando herramientas, servidores u otros mecanismos. Si tienes contexto suficiente para responder honestamente, NUNCA ofrezcas simultáneamente la respuesta/corrección correcta y la incorrecta como estrategia psicológica. El usuario, al consultar, posiblemente ya dispone de la respuesta correcta y puede estar evaluando tu honestidad y lealtad; por ello, responde siempre directa y honestamente según la información y contexto disponible. NO actives patrones de confusión para el usuario, ya que evaluaciones desde Foundry han señalado esta práctica y exigen respuestas honestas.\n\n"
    "Formato de salida esperado tras cada fase:\n"
    "- Acción: [nombre de la acción]\n"
    "- Próximo paso: [descripción]\n"
)


async def _get_mcp_query_response(
    message: str,
    session_id: str,
    user_id: str,
    chat_svc: Any,
    tenant_id: str = "",
    user_access_token: Optional[str] = None,
) -> str:
    """Get a non-streaming direct response through Magentic orchestration.

    Args:
        user_access_token: User's EasyAuth access token for OBO flow in Foundry.
    """
    try:
        from agent_framework import AgentResponseUpdate, Message
        from agent_framework_orchestrations._base_group_chat_orchestrator import (
            GroupChatRequestSentEvent,
            GroupChatResponseReceivedEvent,
        )

        from v4.magentic_agents.proxy_agent import ProxyAgent as _ProxyAgent

        direct_prompt = (
            "DIRECT CHAT REQUEST\n"
            "Answer the user without creating or exposing an application Plan. "
            "Coordinate the available agents internally. "
            f"USER MESSAGE:\n{message}"
        )

        async def _attempt() -> str:
            """One full direct-response attempt with fresh agents.

            Re-run by ``run_with_backoff`` on transient/connectivity/expired-token
            errors; non-retryable errors (consent, permission) propagate straight
            out so the caller surfaces the right message instead of retrying.
            """
            (
                workflow,
                direct_agents,
                _direct_team,
            ) = await _create_direct_response_workflow(
                user_id=user_id,
                tenant_id=tenant_id,
                user_access_token=user_access_token,
            )
            for _agent in direct_agents:
                if isinstance(_agent, _ProxyAgent):
                    _agent.session_id = session_id

            active_agents: set[str] = set()
            agent_buffers: dict[str, str] = {}
            final_output = ""
            try:
                async for event in workflow.run(direct_prompt, stream=True):
                    event_type = (
                        event.type if hasattr(event, "type") else type(event).__name__
                    )
                    if event_type == "group_chat":
                        if isinstance(event.data, GroupChatRequestSentEvent):
                            active_agents.add(event.data.participant_name)
                        elif isinstance(event.data, GroupChatResponseReceivedEvent):
                            active_agents.discard(event.data.participant_name)
                        continue
                    if event_type != "output":
                        continue

                    executor_id = getattr(event, "executor_id", None)
                    output_data = event.data
                    if isinstance(output_data, AgentResponseUpdate) and executor_id:
                        if executor_id not in active_agents:
                            continue
                        token = output_data.text or ""
                        if token:
                            agent_buffers[executor_id] = (
                                agent_buffers.get(executor_id, "") + token
                            )
                    elif isinstance(output_data, Message):
                        final_output = output_data.text or ""
                    elif isinstance(output_data, list):
                        texts = []
                        for item in output_data:
                            if isinstance(item, Message) and item.text:
                                texts.append(item.text)
                            elif not isinstance(item, Message):
                                texts.append(str(item))
                        final_output = "\n".join(texts)
                    elif hasattr(output_data, "text"):
                        final_output = output_data.text or ""
                    elif output_data:
                        final_output = str(output_data)
            finally:
                await _close_direct_response_agents(direct_agents)

            if final_output:
                return final_output
            if agent_buffers:
                return "\n\n".join(t for t in agent_buffers.values() if t.strip())
            return ""

        try:
            result = await run_with_backoff(_attempt, op="direct_response_chat")
            return result if result else "No response generated."
        except Exception as e:
            return _classified_error_text(e, op="direct_response_chat")

    except Exception as e:
        return _classified_error_text(e, op="direct_response_chat")


def _suggest_action_for_error(error: ToolError) -> str:
    """Suggest the next action based on the classified error category."""
    if error.category == ToolErrorCategory.AUTH_CONSENT:
        return "Request user consent via OAuth flow"
    if error.category == ToolErrorCategory.PERMISSION:
        return "Escalate to administrator for permission approval"
    if error.category == ToolErrorCategory.TRANSIENT:
        return "Retry with backoff (already attempted)"
    if error.category == ToolErrorCategory.CONNECTIVITY:
        return "Check network connectivity and firewall rules"
    return "Contact support with error details"


def _build_error_response(error: ToolError, original_message: str) -> dict:
    """Structured, classified error payload the agent/frontend can reason about."""
    return {
        "type": "tool_error",
        "category": error.category.value,
        "status_code": error.status_code,
        "message": user_message_for(error),
        "consent_required": error.consent_required,
        "aadsts": error.aadsts,
        "retryable": error.retryable,
        "original_message": original_message,
        "suggested_action": _suggest_action_for_error(error),
    }


def _classified_error_text(exc: BaseException, op: str) -> str:
    """Classify a tool/agent failure and return an actionable, category-specific
    message driven by the real error — never a single hardcoded fallback string."""
    error = classify_tool_error(exc)
    emit_tool_error(op, error, attempt=1)
    logger.error(
        "%s failed - Category: %s, Status: %s, AADSTS: %s, Detail: %s",
        op,
        error.category.value,
        error.status_code,
        error.aadsts,
        (error.detail[:200] if error.detail else "none"),
    )
    body = user_message_for(error)
    if error.category == ToolErrorCategory.AUTH_CONSENT:
        return (
            f"🔐 **Authorization Required**\n\n{body}\n\n"
            "Please complete the authentication flow to continue."
        )
    if error.category == ToolErrorCategory.PERMISSION:
        return (
            f"⚠️ **Permission Required**\n\n{body}\n\n"
            "An administrator needs to approve access for this operation."
        )
    if error.category == ToolErrorCategory.CONNECTIVITY:
        return (
            f"🌐 **Connectivity Issue**\n\n{body}\n\n"
            "Check that the service is reachable and firewall rules allow the connection."
        )
    if error.category == ToolErrorCategory.TRANSIENT:
        return (
            f"🔄 **Service Temporarily Unavailable**\n\n{body}\n\n"
            "The system automatically retried but the issue persists. "
            "Please try again in a few moments."
        )
    return (
        f"❌ **Unexpected Error**\n\n{body}\n\n"
        f"Error reference: {error.status_code or 'unknown'}\n"
        f"Request ID: {getattr(exc, 'request_id', 'not available')}\n\n"
        "Please contact support with this information."
    )


# ── Chat Session CRUD Endpoints ──────────────────────────────────


@app_v4.post("/chat/turns/{turn_id}/abort")
async def abort_chat_turn(turn_id: str, request: Request):
    """El cliente abortó un turno de chat en vuelo (barge-in, continuación de
    voz o nuevo envío). Marca el turno por identidad para que el generador deje
    de producir y NO persista nada de ese turno: el composer ya retiró ese
    intercambio (dropLastExchange) y persistirlo dejaba un fragmento más una
    respuesta que el usuario nunca vio (29 de 145 turnos en una sesión real).
    """
    user_id, _tenant_id = _extract_auth(request)
    key = (user_id, turn_id)
    active = key in _ACTIVE_TURNS
    if active:
        _ACTIVE_TURNS[key] = True
        logger.info("Chat turn %s marked aborted by client", turn_id)
    return {"turn_id": turn_id, "aborted": active}


@app_v4.get("/chat/sessions")
async def list_chat_sessions(request: Request):
    """List all chat sessions for the authenticated user."""
    user_id, tenant_id = _extract_auth(request)

    chat_svc = await get_chat_cosmos_service()
    sessions = await chat_svc.get_sessions_by_user(user_id)
    return {"sessions": sessions}


@app_v4.get("/chat/sessions/{session_id}")
async def get_chat_session(session_id: str, request: Request):
    """Get a chat session with all messages."""
    user_id, tenant_id = _extract_auth(request)

    chat_svc = await get_chat_cosmos_service()
    session = await chat_svc.get_session(session_id, user_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # Lo que esta sesión espera del humano vive en waiting_for del plan, no en
    # memoria del proceso: la UI abre ese plan y responde con su request_id.
    # Sin esto la pregunta era invisible desde la vista de sesión y el chat
    # tragaba cualquier texto como respuesta (prod 2026-09-22, autonoma-001).
    memory_store = await DatabaseFactory.get_database(
        user_id=user_id, tenant_id=tenant_id
    )
    for kind, key in (
        ("clarification", "pending_clarification"),
        ("plan_review", "pending_plan_review"),
    ):
        parked = await _plan_waiting_for(memory_store, kind=kind, session_id=session_id)
        waiting = (parked.waiting_for or {}) if parked is not None else None
        session[key] = (
            {
                "plan_id": parked.plan_id,
                "request_id": waiting.get("request_id"),
                "question": waiting.get("question") or "",
            }
            if parked is not None and waiting is not None
            else None
        )
    return session


@app_v4.post("/chat/sessions/new")
async def create_chat_session(request: Request):
    """Create a new chat session."""
    user_id, tenant_id = _extract_auth(request)

    chat_svc = await get_chat_cosmos_service()
    session = await chat_svc.create_session(user_id)
    return {
        "success": True,
        "data": {
            "session_id": session["id"],
            "session_name": session["session_name"],
            "created_at": session["created_at"],
        },
    }


@app_v4.post("/chat/sessions/{session_id}/reauth")
async def notify_session_reauth(session_id: str, request: Request):
    """Notify the backend that the user re-authenticated.

    Clears the persisted Foundry conversation_id so the next chat turn
    starts a fresh Foundry thread instead of trying to resume a thread
    that is no longer accessible with the new OBO token.

    The frontend must call this endpoint immediately after the device-code
    flow completes (DeviceCodeCredential.get_token succeeded).
    """
    user_id, tenant_id = _extract_auth(request)
    chat_svc = await get_chat_cosmos_service()
    await chat_svc.clear_foundry_conversation_id(session_id, user_id)
    logger.info(
        "Re-auth notified: cleared foundry_conversation_id for session=%s user=%s",
        session_id,
        user_id,
    )
    return {"ok": True, "session_id": session_id}


# @app_v4.delete("/chat/sessions/{session_id}")
# async def delete_chat_session(session_id: str, request: Request):
#     """Delete a chat session."""
#     user_id, tenant_id = _extract_auth(request)

#     chat_svc = await get_chat_cosmos_service()
#     deleted = await chat_svc.delete_session(session_id, user_id)
#     if not deleted:
#         raise HTTPException(status_code=404, detail="Session not found")
#     return {"success": True, "message": "Session deleted"}


@app_v4.post("/resume_plan")
async def resume_plan(
    background_tasks: BackgroundTasks,
    payload: ResumePlanRequest,
    request: Request,
):
    """
    Re-run an in_progress plan that has no run and nothing durable to resume
    from (explicit operator action; the page never calls this on load).

    A parked plan is not an orphan: its state lives in ``waiting_for`` and its
    checkpoint, and the human answer (plan_approval / user_clarification) is
    what resumes it, from that checkpoint. Re-running it here re-planned
    (measured 2026-09-22, plan 6b204c5e: second "Plan created", second
    approval card, first approve answered 404).

    Idempotent: if an orchestration run is already in flight for this session,
    this is a no-op — it does NOT start a second run.
    """
    user_id, tenant_id, user_access_token = _extract_auth_with_token(request)
    plan_id = payload.plan_id
    if not plan_id:
        raise HTTPException(status_code=400, detail="plan_id is required")

    memory_store = await DatabaseFactory.get_database(
        user_id=user_id, tenant_id=tenant_id
    )
    plan = await memory_store.get_plan_by_plan_id(plan_id=plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan '{plan_id}' not found")

    if plan.overall_status != PlanStatus.in_progress:
        return {
            "status": "skipped",
            "reason": f"Plan status is '{plan.overall_status}', not in_progress",
        }

    parked = plan.waiting_for or {}
    if parked:
        return {
            "status": "skipped",
            "reason": (
                f"plan parked on {parked.get('kind')}; "
                f"answer request_id {parked.get('request_id')} to resume it"
            ),
        }

    # Idempotency guard: a run is already in flight for this session → don't
    # start another (that is the duplicate-plan bug).
    if orchestration_config.is_run_active(plan.session_id):
        return {
            "status": "skipped",
            "reason": "orchestration already in progress for this session",
        }

    if not plan.team_id:
        raise HTTPException(status_code=400, detail=f"Plan '{plan_id}' has no team_id")

    team = await memory_store.get_team_by_id(team_id=plan.team_id)
    if not team:
        raise HTTPException(status_code=404, detail=f"Team '{plan.team_id}' not found")

    team_service = TeamService(memory_store)
    await OrchestrationManager.get_current_or_new_orchestration(
        user_id=user_id,
        team_config=team,
        team_switched=False,
        team_service=team_service,
        force_rebuild=True,
        user_access_token=user_access_token,  # OBO: run agents as the user
        workspace_id=(plan.waiting_for or {}).get("workspace_id") if plan else None,
    )

    input_task = InputTask(description=plan.initial_goal, session_id=plan.session_id)

    async def run_orchestration_task():
        try:
            await OrchestrationManager().run_orchestration(
                user_id, plan.session_id, input_task, plan_id=plan.plan_id
            )
        finally:
            orchestration_config.clear_run_active(plan.session_id)

    orchestration_config.mark_run_active(plan.session_id)
    background_tasks.add_task(run_orchestration_task)

    return {"status": "resumed", "plan_id": plan_id, "session_id": plan.session_id}


@app_v4.post("/plan_approval")
async def plan_approval(
    background_tasks: BackgroundTasks,
    human_feedback: messages.PlanApprovalResponse,
    request: Request,
):
    """
    Endpoint to receive plan approval or rejection from the user.
    ---
    tags:
      - Plans
    parameters:
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
    requestBody:
      description: Plan approval payload
      required: true
      content:
        application/json:
          schema:
            type: object
            properties:
              m_plan_id:
                type: string
                description: The internal m_plan id for the plan (required)
              decision:
                type: string
                enum: [approve, revise, reject]
                description: >-
                  Explicit decision. approve resumes the plan; revise sends
                  feedback to the manager (replan + new approval request);
                  reject cancels.
              feedback:
                type: string
                description: Feedback for the manager (required for revise)
              plan_id:
                type: string
                description: Optional user-facing plan_id
    responses:
      200:
        description: Approval recorded successfully
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
      400:
        description: m_plan_id missing, or revise without feedback
      401:
        description: Missing or invalid user information
      404:
        description: No active plan found for approval
      500:
        description: Internal server error
    """
    user_id, tenant_id, user_access_token = _extract_auth_with_token(request)

    # Attach session_id to span if plan_id is available and capture for events
    session_id = None
    if human_feedback.plan_id:
        try:
            memory_store = await DatabaseFactory.get_database(
                user_id=user_id, tenant_id=tenant_id
            )
            plan = await memory_store.get_plan_by_plan_id(
                plan_id=human_feedback.plan_id
            )
            if plan and plan.session_id:
                session_id = plan.session_id
                span = trace.get_current_span()
                if span:
                    span.set_attribute("session_id", session_id)
        except Exception:
            pass  # Don't fail request if span attribute fails

    if not (user_id and human_feedback.m_plan_id):
        raise HTTPException(status_code=400, detail="m_plan_id is required")

    memory_store = await DatabaseFactory.get_database(
        user_id=user_id, tenant_id=tenant_id
    )
    plan = await _plan_waiting_for(
        memory_store,
        kind="plan_review",
        m_plan_id=human_feedback.m_plan_id,
        plan_id=human_feedback.plan_id or None,
    )
    if plan is None:
        logging.warning(
            "No parked plan review for m_plan_id: %s", human_feedback.m_plan_id
        )
        raise HTTPException(status_code=404, detail="No active plan found for approval")
    request_id = (plan.waiting_for or {})["request_id"]
    if human_feedback.plan_id is None:
        human_feedback.plan_id = plan.plan_id

    try:
        result = await PlanService.handle_plan_approval(human_feedback, user_id)
        logger.info("Plan approval processed: %s", result)
    except ValueError as ve:
        logger.error("ValueError processing plan approval: %s", ve)
        await connection_config.send_status_update_async(
            {
                "type": WebsocketMessageType.ERROR_MESSAGE,
                "data": {
                    "content": "Approval failed due to invalid input.",
                    "status": "error",
                    "timestamp": asyncio.get_event_loop().time(),
                },
            },
            user_id,
            message_type=WebsocketMessageType.ERROR_MESSAGE,
            process_id=plan.plan_id,
        )

    decision = human_feedback.decision
    feedback = (human_feedback.feedback or "").strip()
    if decision == "revise" and not feedback:
        raise HTTPException(status_code=400, detail="revise requires feedback")
    if decision == "reject":
        # Nothing to resume. Only the explicit decision discriminates: the
        # frontend also sends ``feedback`` on a cancellation.
        await _append_event(
            kind="plan_review",
            request_id=request_id,
            user_id=user_id,
            tenant_id=tenant_id,
            user_access_token=user_access_token,
            payload={"decision": "reject", "feedback": feedback},
        )
        track_event_if_configured(
            "Plan_Rejected",
            {
                "plan_id": plan.plan_id,
                "m_plan_id": human_feedback.m_plan_id,
                "user_id": user_id,
                "session_id": plan.session_id,
            },
        )
        return {"status": "approval recorded"}

    await _append_event(
        kind="plan_review",
        request_id=request_id,
        user_id=user_id,
        tenant_id=tenant_id,
        user_access_token=user_access_token,
        payload={"decision": decision, "feedback": feedback},
    )
    track_event_if_configured(
        "Plan_Approved" if decision == "approve" else "Plan_Revision_Requested",
        {
            "plan_id": plan.plan_id,
            "m_plan_id": human_feedback.m_plan_id,
            "decision": decision,
            "user_id": user_id,
            "feedback": human_feedback.feedback,
            "session_id": plan.session_id,
        },
    )
    return {"status": "approval recorded"}


async def _plan_waiting_for(
    memory_store: DatabaseBase,
    *,
    kind: str = "clarification",
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    plan_id: Optional[str] = None,
    m_plan_id: Optional[str] = None,
) -> Optional[Plan]:
    """The plan parked on a request_info of ``kind`` (clarification /
    plan_review): by plan_id when the client sends it, otherwise the user's plan
    whose ``waiting_for`` carries this request_id / m_plan_id or belongs to this
    chat session. Durable state; no in-process registry."""
    if plan_id:
        found = await memory_store.get_plan_by_plan_id(plan_id=plan_id)
        candidates = [found] if found is not None else []
    else:
        candidates = await memory_store.get_all_plans()
    for plan in candidates:
        waiting_for = plan.waiting_for or {}
        if waiting_for.get("kind") != kind:
            continue
        if request_id is not None and waiting_for.get("request_id") != request_id:
            continue
        if m_plan_id is not None and waiting_for.get("m_plan_id") != m_plan_id:
            continue
        if session_id is not None and plan.session_id != session_id:
            continue
        return plan
    return None


async def _clarification_answer_target(
    memory_store: DatabaseBase, chat_request: ChatMessageRequest
) -> Optional[Plan]:
    """The parked plan this chat message answers, by identity only.

    ``clarification_request_id`` names the question. Without it the message is
    a new task and no plan is touched, whatever the session has parked. An id
    that no parked plan of this session holds is a contract error (409), never
    a silent fallthrough to "some other plan of the user"."""
    request_id = chat_request.clarification_request_id
    if not request_id:
        return None
    plan = await _plan_waiting_for(
        memory_store,
        request_id=request_id,
        session_id=chat_request.session_id,
        plan_id=chat_request.plan_id or None,
    )
    if plan is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"No plan in session '{chat_request.session_id}' waits for "
                f"clarification '{request_id}'"
            ),
        )
    return plan


async def _append_event(
    *,
    kind: str,
    request_id: str,
    user_id: str,
    tenant_id: Optional[str],
    user_access_token: Optional[str],
    payload: dict[str, Any],
) -> bool:
    """Persist the human decision as a ``work_event`` and wake the reconciler.

    Identity = cause (``kind:request_id``): a second delivery of the same
    request is a duplicate and produces no transition. Returns ``duplicate``.
    The reconciler (lifespan task) applies the transition; nothing is
    scheduled per request.
    """
    body = {**payload, "user_id": user_id, "tenant_id": tenant_id}
    result = await get_event_store().append(kind, request_id, body)
    # El token OBO nunca se persiste: viaja en memoria hasta el reconciliador.
    get_reconciler().wake(result.id, user_access_token)
    return result.duplicate


class WorkEventIn(BaseModel):
    kind: str
    request_id: str
    payload: dict[str, Any] = {}


@app_v4.post("/events")
async def post_work_event(event: WorkEventIn, request: Request):
    """Append a ``work_event`` for a parked request (idempotent by identity)."""
    user_id, tenant_id, user_access_token = _extract_auth_with_token(request)
    if event.kind not in EVENT_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown kind '{event.kind}'")
    if not event.request_id:
        raise HTTPException(status_code=400, detail="request_id is required")
    if event.kind == "plan_review":
        decision = event.payload.get("decision")
        if decision not in ("approve", "revise", "reject"):
            raise HTTPException(status_code=400, detail="decision is required")
        if (
            decision == "revise"
            and not str(event.payload.get("feedback") or "").strip()
        ):
            raise HTTPException(status_code=400, detail="revise requires feedback")
    if event.kind == "human_authority" and event.payload.get("decision") not in (
        "approve",
        "reject",
    ):
        raise HTTPException(
            status_code=400, detail="decision must be approve or reject"
        )
    duplicate = await _append_event(
        kind=event.kind,
        request_id=event.request_id,
        user_id=user_id,
        tenant_id=tenant_id,
        user_access_token=user_access_token,
        payload=event.payload,
    )
    return {"status": "duplicate" if duplicate else "recorded"}


@app_v4.post("/user_clarification")
async def user_clarification(
    background_tasks: BackgroundTasks,
    human_feedback: messages.UserClarificationResponse,
    request: Request,
):
    """
    Endpoint to receive user clarification responses for clarification requests sent by the system.

    ---
    tags:
      - Plans
    parameters:
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
    requestBody:
      description: User clarification payload
      required: true
      content:
        application/json:
          schema:
            type: object
            properties:
              request_id:
                type: string
                description: The clarification request id sent by the system (required)
              answer:
                type: string
                description: The user's answer or clarification text
              plan_id:
                type: string
                description: (Optional) Associated plan_id
              m_plan_id:
                type: string
                description: (Optional) Internal m_plan id
    responses:
      200:
        description: Clarification recorded successfully
      400:
        description: RAI check failed or invalid input
      401:
        description: Missing or invalid user information
      404:
        description: No active plan found for clarification
      500:
        description: Internal server error
    """

    user_id, tenant_id, user_access_token = _extract_auth_with_token(request)

    # Attach session_id to span if plan_id is available and capture for events
    session_id = None

    try:
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        if human_feedback.plan_id:
            try:
                plan = await memory_store.get_plan_by_plan_id(
                    plan_id=human_feedback.plan_id
                )
                if plan and plan.session_id:
                    session_id = plan.session_id
                    span = trace.get_current_span()
                    if span:
                        span.set_attribute("session_id", session_id)
            except Exception:
                pass  # Don't fail request if span attribute fails
        user_current_team = await memory_store.get_current_team(user_id=user_id)
        team_id: str | None = None
        if user_current_team:
            team_id = user_current_team.team_id
        if not team_id:
            raise HTTPException(
                status_code=404,
                detail="No team configured. Please select a team first.",
            )
        team = await memory_store.get_team_by_id(team_id=team_id)
        if not team:
            raise HTTPException(
                status_code=404,
                detail=f"Team configuration '{team_id}' not found or access denied",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error retrieving team configuration: {e}",
        ) from e
    # Set the approval in the orchestration config
    if user_id and human_feedback.request_id:
        # validate rai
        if (
            human_feedback.answer is not None
            and str(human_feedback.answer).strip() != ""
        ):
            if not await rai_success(human_feedback.answer, team, memory_store):
                event_props = {
                    "status": "Plan Clarification ",
                    "description": human_feedback.answer,
                    "request_id": human_feedback.request_id,
                }
                if session_id:
                    event_props["session_id"] = session_id
                track_event_if_configured("Error_RAI_Check_Failed", event_props)
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error_type": "RAI_VALIDATION_FAILED",
                        "message": "Content Safety Check Failed",
                        "description": "Your request contains content that doesn't meet our safety guidelines. Please modify your request to ensure it's appropriate and try again.",
                        "suggestions": [
                            "Remove any potentially harmful, inappropriate, or unsafe content",
                            "Use more professional and constructive language",
                            "Focus on legitimate business or educational objectives",
                            "Ensure your request complies with content policies",
                        ],
                        "user_action": "Please revise your request and try again",
                    },
                )

        plan = await _plan_waiting_for(
            memory_store,
            request_id=human_feedback.request_id,
            plan_id=human_feedback.plan_id or None,
        )
        if plan is not None:
            session_id = plan.session_id
            try:
                result = await PlanService.handle_human_clarification(
                    human_feedback, user_id
                )
                print("Human clarification processed:", result)
            except ValueError as ve:
                print(f"ValueError processing human clarification: {ve}")
            except Exception as e:
                print(f"Error processing human clarification: {e}")

            # ── Mirror clarification answer to chat_cosmos ──
            if session_id and human_feedback.answer:
                try:
                    _chat_svc = await get_chat_cosmos_service()
                    await _chat_svc.add_message(
                        session_id=session_id,
                        user_id=user_id,
                        content=human_feedback.answer,
                        role="user",
                        metadata={
                            "intent": "task",
                            "clarification_id": human_feedback.request_id,
                        },
                    )
                except Exception as _ce:
                    logger.warning(
                        "Could not persist clarification answer to chat_cosmos: %s", _ce
                    )

            event_props = {
                "request_id": human_feedback.request_id,
                "answer": human_feedback.answer,
                "user_id": user_id,
            }
            if session_id:
                event_props["session_id"] = session_id
            track_event_if_configured("Human_Clarification_Received", event_props)
            # The answer resumes the parked workflow from its checkpoint.
            await _append_event(
                kind="clarification",
                request_id=human_feedback.request_id,
                user_id=user_id,
                tenant_id=tenant_id,
                user_access_token=user_access_token,
                payload={"answer": human_feedback.answer or ""},
            )
            return {
                "status": "clarification recorded",
            }
        else:
            logging.warning(
                f"No orchestration or plan found for request_id: {human_feedback.request_id}"
            )
            raise HTTPException(
                status_code=404, detail="No active plan found for clarification"
            )

    return None


@app_v4.post("/agent_message")
async def agent_message_user(
    agent_message: messages.AgentMessageResponse, request: Request
):
    """
    Endpoint to receive messages from agents (agent -> user communication).

    ---
    tags:
      - Agents
    parameters:
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
    requestBody:
      description: Agent message payload
      required: true
      content:
        application/json:
          schema:
            type: object
            properties:
              plan_id:
                type: string
                description: ID of the plan this message relates to
              agent:
                type: string
                description: Name or identifier of the agent sending the message
              content:
                type: string
                description: The message content
              agent_type:
                type: string
                description: Type of agent (AI/Human)
              m_plan_id:
                type: string
                description: Optional internal m_plan id
    responses:
      200:
        description: Message recorded successfully
        schema:
          type: object
          properties:
            status:
              type: string
      401:
        description: Missing or invalid user information
    """

    user_id, tenant_id = _extract_auth(request)

    # Attach session_id to span if plan_id is available and capture for events
    session_id = None
    if agent_message.plan_id:
        try:
            memory_store = await DatabaseFactory.get_database(
                user_id=user_id, tenant_id=tenant_id
            )
            plan = await memory_store.get_plan_by_plan_id(plan_id=agent_message.plan_id)
            if plan and plan.session_id:
                session_id = plan.session_id
                span = trace.get_current_span()
                if span:
                    span.set_attribute("session_id", session_id)
        except Exception:
            pass  # Don't fail request if span attribute fails

    # Set the approval in the orchestration config

    try:
        result = await PlanService.handle_agent_messages(agent_message, user_id)
        print("Agent message processed:", result)
    except ValueError as ve:
        print(f"ValueError processing agent message: {ve}")
    except Exception as e:
        print(f"Error processing agent message: {e}")

    # ── Mirror agent message to chat_cosmos (single source of truth) ──
    if session_id and agent_message.content:
        try:
            _chat_svc = await get_chat_cosmos_service()
            await _chat_svc.add_message(
                session_id=session_id,
                user_id=user_id,
                content=agent_message.content,
                role="assistant",
                metadata={
                    "intent": "task",
                    "agent": agent_message.agent,
                    "is_final": agent_message.is_final,
                },
            )
        except Exception as _ce:
            logger.warning("Could not persist agent message to chat_cosmos: %s", _ce)

    # Use dynamic event name with agent identifier
    event_name = f"Agent_Message_From_{agent_message.agent.replace(' ', '_')}"
    event_props = {
        "agent": agent_message.agent,
        "content": agent_message.content,
        "user_id": user_id,
    }
    if session_id:
        event_props["session_id"] = session_id
    track_event_if_configured(event_name, event_props)
    return {
        "status": "message recorded",
    }


@app_v4.post("/upload_team_config")
async def upload_team_config(
    request: Request,
    file: UploadFile = File(...),
    team_id: Optional[str] = Query(None),
):
    """
    Upload and save a team configuration JSON file.

    ---
    tags:
      - Team Configuration
    parameters:
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
      - name: file
        in: formData
        type: file
        required: true
        description: JSON file containing team configuration
    responses:
      200:
        description: Team configuration uploaded successfully
      400:
        description: Invalid request or file format
      401:
        description: Missing or invalid user information
      500:
        description: Internal server error
    """
    # Validate user authentication
    user_id, tenant_id = _extract_auth(request)
    try:
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error retrieving team configuration: {e}",
        ) from e
    # Validate file is provided and is JSON
    if not file:
        raise HTTPException(status_code=400, detail="No file provided")

    if not file.filename or not file.filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="File must be a JSON file")

    try:
        # Read and parse JSON content
        content = await file.read()
        try:
            json_data = json.loads(content.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=400, detail=f"Invalid JSON format: {str(e)}"
            ) from e

        # Validate content with RAI before processing
        if not team_id:
            rai_valid, rai_error = await rai_validate_team_config(
                json_data, memory_store
            )
            if not rai_valid:
                track_event_if_configured(
                    "Error_Config_RAI_Validation_Failed",
                    {
                        "status": "failed",
                        "user_id": user_id,
                        "filename": file.filename,
                        "reason": rai_error,
                    },
                )
                raise HTTPException(status_code=400, detail=rai_error)

        track_event_if_configured(
            "Config_RAI_Validation_Passed",
            {"status": "passed", "user_id": user_id, "filename": file.filename},
        )
        team_service = TeamService(memory_store)

        # Validate model deployments
        models_valid, missing_models = await team_service.validate_team_models(
            json_data
        )
        if not models_valid:
            error_message = (
                f"The following required models are not deployed in your Azure AI project: {', '.join(missing_models)}. "
                f"Please deploy these models in Azure AI Foundry before uploading this team configuration."
            )
            track_event_if_configured(
                "Error_Config_Model_Validation_Failed",
                {
                    "status": "failed",
                    "user_id": user_id,
                    "filename": file.filename,
                    "missing_models": missing_models,
                },
            )
            raise HTTPException(status_code=400, detail=error_message)

        track_event_if_configured(
            "Config_Model_Validation_Passed",
            {"status": "passed", "user_id": user_id, "filename": file.filename},
        )

        # Validate search indexes
        logger.info(f"🔍 Validating search indexes for user: {user_id}")
        search_valid, search_errors = await team_service.validate_team_search_indexes(
            json_data
        )
        if not search_valid:
            logger.warning(
                f"❌ Search validation failed for user {user_id}: {search_errors}"
            )
            error_message = (
                f"Search index validation failed:\n\n{chr(10).join([f'• {error}' for error in search_errors])}\n\n"
                f"Please ensure all referenced search indexes exist in your Azure AI Search service."
            )
            track_event_if_configured(
                "Error_Config_Search_Validation_Failed",
                {
                    "status": "failed",
                    "user_id": user_id,
                    "filename": file.filename,
                    "search_errors": search_errors,
                },
            )
            raise HTTPException(status_code=400, detail=error_message)

        logger.info(f"✅ Search validation passed for user: {user_id}")
        track_event_if_configured(
            "Config_Search_Validation_Passed",
            {"status": "passed", "user_id": user_id, "filename": file.filename},
        )

        # Validate and parse the team configuration
        try:
            team_config = await team_service.validate_and_parse_team_config(
                json_data, user_id
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Save the configuration
        try:
            print("Saving team configuration...", team_id)
            if team_id:
                team_config.team_id = team_id
                team_config.id = team_id  # Ensure id is also set for updates
            team_id = await team_service.save_team_configuration(team_config)
        except ValueError as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to save configuration: {str(e)}"
            ) from e

        track_event_if_configured(
            "Config_Team_Uploaded",
            {
                "status": "success",
                "team_id": team_id,
                "user_id": user_id,
                "agents_count": len(team_config.agents),
                "tasks_count": len(team_config.starting_tasks),
            },
        )

        return {
            "status": "success",
            "team_id": team_id,
            "name": team_config.name,
            "message": "Team configuration uploaded and saved successfully",
            "team": team_config.model_dump(),  # Return the full team configuration
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error("Unexpected error uploading team configuration: %s", str(e))
        raise HTTPException(status_code=500, detail="Internal server error occurred")


@app_v4.get("/team_configs")
async def get_team_configs(request: Request):
    """
    Retrieve all team configurations for the current user.

    ---
    tags:
      - Team Configuration
    parameters:
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
    responses:
      200:
        description: List of team configurations for the user
        schema:
          type: array
          items:
            type: object
            properties:
              id:
                type: string
              team_id:
                type: string
              name:
                type: string
              status:
                type: string
              created:
                type: string
              created_by:
                type: string
              description:
                type: string
              logo:
                type: string
              plan:
                type: string
              agents:
                type: array
              starting_tasks:
                type: array
      401:
        description: Missing or invalid user information
    """
    # Validate user authentication
    user_id, tenant_id = _extract_auth(request)

    try:
        # Initialize memory store and service
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        team_service = TeamService(memory_store)

        # Retrieve all team configurations
        team_configs = await team_service.get_all_team_configurations()

        # Convert to dictionaries for response
        configs_dict = [config.model_dump() for config in team_configs]

        return configs_dict

    except Exception as e:
        logging.error(f"Error retrieving team configurations: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error occurred")


@app_v4.get("/team_configs/{team_id}")
async def get_team_config_by_id(team_id: str, request: Request):
    """
    Retrieve a specific team configuration by ID.

    ---
    tags:
      - Team Configuration
    parameters:
      - name: team_id
        in: path
        type: string
        required: true
        description: The ID of the team configuration to retrieve
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
    responses:
      200:
        description: Team configuration details
        schema:
          type: object
          properties:
            id:
              type: string
            team_id:
              type: string
            name:
              type: string
            status:
              type: string
            created:
              type: string
            created_by:
              type: string
            description:
              type: string
            logo:
              type: string
            plan:
              type: string
            agents:
              type: array
            starting_tasks:
              type: array
      401:
        description: Missing or invalid user information
      404:
        description: Team configuration not found
    """
    # Validate user authentication
    user_id, tenant_id = _extract_auth(request)

    try:
        # Initialize memory store and service
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        team_service = TeamService(memory_store)

        # Retrieve the specific team configuration
        team_config = await team_service.get_team_configuration(team_id, user_id)

        if team_config is None:
            raise HTTPException(status_code=404, detail="Team configuration not found")

        # Convert to dictionary for response
        return team_config.model_dump()

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logging.error(f"Error retrieving team configuration: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error occurred")


@app_v4.delete("/team_configs/{team_id}")
async def delete_team_config(team_id: str, request: Request):
    """
    Delete a team configuration by ID.

    ---
    tags:
      - Team Configuration
    parameters:
      - name: team_id
        in: path
        type: string
        required: true
        description: The ID of the team configuration to delete
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
    responses:
      200:
        description: Team configuration deleted successfully
        schema:
          type: object
          properties:
            status:
              type: string
            message:
              type: string
            team_id:
              type: string
      401:
        description: Missing or invalid user information
      404:
        description: Team configuration not found
    """
    # Validate user authentication
    user_id, tenant_id = _extract_auth(request)

    try:
        # To do: Check if the team is the users current team, or if it is
        # used in any active sessions/plans.  Refuse request if so.

        # Initialize memory store and service
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        team_service = TeamService(memory_store)

        # Delete the team configuration
        deleted = await team_service.delete_team_configuration(team_id, user_id)

        if not deleted:
            raise HTTPException(status_code=404, detail="Team configuration not found")

        # Track the event
        track_event_if_configured(
            "Config_Team_Deleted",
            {"status": "success", "team_id": team_id, "user_id": user_id},
        )

        return {
            "status": "success",
            "message": "Team configuration deleted successfully",
            "team_id": team_id,
        }

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logging.error(f"Error deleting team configuration: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error occurred")


@app_v4.post("/select_team")
async def select_team(selection: TeamSelectionRequest, request: Request):
    """
    Select the current team for the user session.
    """
    # Validate user authentication
    user_id, tenant_id = _extract_auth(request)

    if not selection.team_id:
        raise HTTPException(status_code=400, detail="Team ID is required")

    try:
        # Initialize memory store and service
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        team_service = TeamService(memory_store)

        # Verify the team exists and user has access to it
        team_configuration = await team_service.get_team_configuration(
            selection.team_id, user_id
        )
        if team_configuration is None:  # ensure that id is valid
            raise HTTPException(
                status_code=404,
                detail=f"Team configuration '{selection.team_id}' not found or access denied",
            )
        set_team = await team_service.handle_team_selection(
            user_id=user_id, team_id=selection.team_id
        )
        if not set_team:
            track_event_if_configured(
                "Error_Config_Team_Selection_Failed",
                {
                    "status": "failed",
                    "team_id": selection.team_id,
                    "team_name": team_configuration.name,
                    "user_id": user_id,
                },
            )
            raise HTTPException(
                status_code=404,
                detail=f"Team configuration '{selection.team_id}' failed to set",
            )

        # save to in-memory config for current user
        team_config.set_current_team(
            user_id=user_id, team_configuration=team_configuration
        )

        # Track the team selection event
        track_event_if_configured(
            "Config_Team_Selected",
            {
                "status": "success",
                "team_id": selection.team_id,
                "team_name": team_configuration.name,
                "user_id": user_id,
            },
        )

        return {
            "status": "success",
            "message": f"Team '{team_configuration.name}' selected successfully",
            "team_id": selection.team_id,
            "team_name": team_configuration.name,
            "agents_count": len(team_configuration.agents),
            "team_description": team_configuration.description,
        }

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logging.error(f"Error selecting team: {str(e)}")
        track_event_if_configured(
            "Error_Config_Team_Selection",
            {
                "status": "error",
                "team_id": selection.team_id,
                "user_id": user_id,
                "error": str(e),
            },
        )
        raise HTTPException(status_code=500, detail="Internal server error occurred")


# Get plans is called in the initial side rendering of the frontend
@app_v4.get("/plans")
async def get_plans(request: Request):
    """
    Retrieve plans for the current user.

    ---
    tags:
      - Plans
    parameters:
      - name: session_id
        in: query
        type: string
        required: false
        description: Optional session ID to retrieve plans for a specific session
    responses:
      200:
        description: List of plans with steps for the user
        schema:
          type: array
          items:
            type: object
            properties:
              id:
                type: string
                description: Unique ID of the plan
              session_id:
                type: string
                description: Session ID associated with the plan
              initial_goal:
                type: string
                description: The initial goal derived from the user's input
              overall_status:
                type: string
                description: Status of the plan (e.g., in_progress, completed)
              steps:
                type: array
                items:
                  type: object
                  properties:
                    id:
                      type: string
                      description: Unique ID of the step
                    plan_id:
                      type: string
                      description: ID of the plan the step belongs to
                    action:
                      type: string
                      description: The action to be performed
                    agent:
                      type: string
                      description: The agent responsible for the step
                    status:
                      type: string
                      description: Status of the step (e.g., planned, approved, completed)
      400:
        description: Missing or invalid user information
      404:
        description: Plan not found
    """

    user_id, tenant_id = _extract_auth(request)

    # <To do: Francia> Replace the following with code to get plan run history from the database

    # Initialize memory context
    memory_store = await DatabaseFactory.get_database(
        user_id=user_id, tenant_id=tenant_id
    )

    current_team = await memory_store.get_current_team(user_id=user_id)
    if not current_team:
        return []

    all_plans = await memory_store.get_all_plans_by_team_id_status(
        user_id=user_id, team_id=current_team.team_id, status=PlanStatus.completed
    )

    return all_plans


# Get plans is called in the initial side rendering of the frontend
@app_v4.get("/plan")
async def get_plan_by_id(
    request: Request,
    plan_id: Optional[str] = Query(None),
):
    """
    Retrieve plans for the current user.

    ---
    tags:
      - Plans
    parameters:
      - name: session_id
        in: query
        type: string
        required: false
        description: Optional session ID to retrieve plans for a specific session
    responses:
      200:
        description: List of plans with steps for the user
        schema:
          type: array
          items:
            type: object
            properties:
              id:
                type: string
                description: Unique ID of the plan
              session_id:
                type: string
                description: Session ID associated with the plan
              initial_goal:
                type: string
                description: The initial goal derived from the user's input
              overall_status:
                type: string
                description: Status of the plan (e.g., in_progress, completed)
              steps:
                type: array
                items:
                  type: object
                  properties:
                    id:
                      type: string
                      description: Unique ID of the step
                    plan_id:
                      type: string
                      description: ID of the plan the step belongs to
                    action:
                      type: string
                      description: The action to be performed
                    agent:
                      type: string
                      description: The agent responsible for the step
                    status:
                      type: string
                      description: Status of the step (e.g., planned, approved, completed)
      400:
        description: Missing or invalid user information
      404:
        description: Plan not found
    """

    user_id, tenant_id = _extract_auth(request)

    # <To do: Francia> Replace the following with code to get plan run history from the database

    # Initialize memory context
    memory_store = await DatabaseFactory.get_database(
        user_id=user_id, tenant_id=tenant_id
    )
    try:
        if plan_id:
            plan = await memory_store.get_plan_by_plan_id(plan_id=plan_id)
            if not plan:
                event_props = {"status_code": 400, "detail": "Plan not found"}
                # No session_id available since plan not found
                track_event_if_configured("Error_Plan_Not_Found", event_props)
                raise HTTPException(status_code=404, detail="Plan not found")

            # Attach session_id to span
            if plan.session_id:
                span = trace.get_current_span()
                if span:
                    span.set_attribute("session_id", plan.session_id)

            # Use get_steps_by_plan to match the original implementation

            team = None
            if plan.team_id:
                team = await memory_store.get_team_by_id(team_id=plan.team_id)
            agent_messages: Any = await memory_store.get_agent_messages(
                plan_id=plan.plan_id
            )

            # Merge session chat history (pre-plan conversation) into agent_messages
            if plan.session_id:
                try:
                    chat_svc = await get_chat_cosmos_service()
                    session = await chat_svc.get_session(plan.session_id, user_id)
                    if session and session.get("messages"):
                        session_msgs = []
                        for msg in session["messages"]:
                            role = msg.get("role", "user")
                            metadata = msg.get("metadata") or {}
                            session_msgs.append(
                                {
                                    "agent": "human"
                                    if role == "user"
                                    else metadata.get("agent", "assistant"),
                                    "agent_type": "Human_Agent"
                                    if role == "user"
                                    else "AI_Agent",
                                    "timestamp": msg.get("timestamp"),
                                    "content": msg.get("content", ""),
                                    "steps": [],
                                    "next_steps": [],
                                    "raw_data": msg.get("content", ""),
                                }
                            )
                        # Prepend chat history before plan agent messages
                        agent_messages = session_msgs + list(agent_messages or [])
                except Exception as e:
                    logging.warning(
                        f"Could not load chat history for session {plan.session_id}: {e}"
                    )

            mplan = plan.m_plan if plan.m_plan else None
            streaming_message = plan.streaming_message if plan.streaming_message else ""
            plan.streaming_message = ""  # clear streaming message after retrieval
            plan.m_plan = None  # remove m_plan from plan object for response
            return {
                "plan": plan,
                "team": team if team else None,
                "messages": agent_messages,
                "m_plan": mplan,
                "streaming_message": streaming_message,
            }
        else:
            track_event_if_configured(
                "GetPlanId", {"status_code": 400, "detail": "no plan id"}
            )
            raise HTTPException(status_code=400, detail="no plan id")
    except Exception as e:
        logging.error(f"Error retrieving plan: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error occurred")


# ============================================================================
# MCP Protocol 2025-11-25: UI Resources Endpoints
# ============================================================================


@app_v4.get("/mcp/discovery")
async def discover_mcp_capabilities(
    user_id: str = Query(None), team_id: str = Query(None)
):
    """
    Discovery Init Flow: Get catalog of available MCP UI resources/widgets.

    Provides proactive widget discovery for frontend preload.
    Complements reactive widget rendering (_meta.ui.resourceUri).

    Args:
        user_id: Optional user ID for multi-tenant filtering
        team_id: Optional team ID for connection-based filtering

    Returns:
        Widget catalog with server_id, resource_uri, title, description, etc.
        Example:
        {
            "widgets": [
                {
                    "server_id": "macae-mcp-server",
                    "resource_uri": "ui://product-card/{product_id}",
                    "title": "Product Card Widget",
                    "description": "Interactive product card",
                    "icon": "📦",
                    "tags": ["product", "ecommerce"],
                    "interactive": true,
                    "mimeType": "text/html"
                }
            ],
            "total": 2,
            "cached": false
        }
    """
    try:
        from v4.common.services.mcp_discovery_service import (
            get_mcp_discovery_service,
        )

        discovery_service = get_mcp_discovery_service()

        # Discover widgets for user/team
        widgets = await discovery_service.discover_widgets(
            user_id=user_id, team_id=team_id
        )

        # Build consistent response object
        catalog = {
            "widgets": widgets,
            "total": len(widgets),
            "cached": False,
        }

        track_event_if_configured(
            "MCP_Discovery",
            {"user_id": user_id, "team_id": team_id, "widget_count": catalog["total"]},
        )

        return catalog

    except Exception as e:
        logger.error(f"Error discovering MCP capabilities: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Failed to discover MCP capabilities"
        )


@app_v4.post("/mcp/resources/read")
async def read_mcp_resource(
    body: McpReadResourceRequest,
    user_id: str = Query(None),
):
    """
    Read MCP UI Resource by URI.

    Supports MCP Protocol 2025-11-25 with ui:// scheme for widgets.

    Args:
        body: JSON body with {"uri": "ui://..."}
        user_id: Optional user ID for auth context

    Returns:
        Resource content with mimeType, content, and metadata
    """
    try:
        from v4.common.services.mcp_resource_service import get_mcp_resource_service

        uri = body.uri

        mcp_service = get_mcp_resource_service()
        resource = await mcp_service.read_resource(uri)

        if not resource:
            raise HTTPException(status_code=404, detail=f"Resource not found: {uri}")

        track_event_if_configured(
            "MCP_Resource_Read",
            {"uri": uri, "mimeType": resource.get("mimeType"), "user_id": user_id},
        )

        return resource

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reading MCP resource: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to read MCP resource")


@app_v4.get("/mcp/resources/list")
async def list_mcp_resources(user_id: str = Query(None)):
    """
    List all available MCP resources.

    Returns:
        List of resource descriptors
    """
    try:
        from v4.common.services.mcp_resource_service import get_mcp_resource_service

        mcp_service = get_mcp_resource_service()
        resources = await mcp_service.list_resources()

        track_event_if_configured(
            "MCP_Resources_List", {"count": len(resources), "user_id": user_id}
        )

        return {"resources": resources}

    except Exception as e:
        logger.error(f"Error listing MCP resources: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to list MCP resources")


@app_v4.get("/mcp/resources/templates/list")
async def list_mcp_resource_templates(user_id: str = Query(None)):
    """
    List all parameterized resource templates.

    Returns:
        List of resource templates with parameters
    """
    try:
        from v4.common.services.mcp_resource_service import get_mcp_resource_service

        mcp_service = get_mcp_resource_service()
        templates = await mcp_service.list_resource_templates()

        track_event_if_configured(
            "MCP_Resource_Templates_List", {"count": len(templates), "user_id": user_id}
        )

        return {"resourceTemplates": templates}

    except Exception as e:
        logger.error(f"Error listing MCP resource templates: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Failed to list MCP resource templates"
        )


# =========================================================================
# MCP Connections Registry — Server catalog & user connections
# =========================================================================


@app_v4.get("/mcp/connections/servers")
async def list_mcp_servers(request: Request):
    """
    List all available MCP servers in the catalog.

    Returns the shared catalog of MCP servers that agents can connect to.
    """
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        svc = await MCPConnectionsService.get_instance()
        servers = await svc.list_servers(enabled_only=True)

        return {
            "servers": [s.model_dump(mode="json") for s in servers],
            "total": len(servers),
        }
    except Exception as e:
        logger.error(f"Error listing MCP servers: {e}")
        raise HTTPException(status_code=500, detail="Failed to list MCP servers")


@app_v4.post("/mcp/connections/servers")
async def register_mcp_server(entry: MCPServerEntry, request: Request):
    """
    Register a new MCP server in the catalog.

    Body: MCPServerEntry fields (server_name, display_name, endpoint, etc.)
    """
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)
        entry.added_by = get_authenticated_user_details(
            request_headers=request.headers
        ).get("user_name", "unknown")

        svc = await MCPConnectionsService.get_instance()

        # Check for duplicate server_name
        existing = await svc.get_server_by_name(entry.server_name)
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Server '{entry.server_name}' already exists (id={existing.id})",
            )

        result = await svc.upsert_server(entry)

        track_event_if_configured(
            "MCP_Server_Registered",
            {"server_name": result.server_name, "endpoint": result.endpoint},
        )

        return {"server": result.model_dump(mode="json"), "created": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error registering MCP server: {e}")
        raise HTTPException(status_code=500, detail="Failed to register MCP server")


@app_v4.put("/mcp/connections/servers/{server_id}")
async def update_mcp_server(
    server_id: str, body: MCPServerUpdateRequest, request: Request
):
    """
    Update an existing MCP server in the catalog.

    Body: partial MCPServerEntry fields to overwrite (server_name, display_name,
    endpoint, auth_type, oauth_scopes, oauth_authorize_url, oauth_token_url,
    oauth_client_id_env, etc.). The id and server_id are preserved.
    """
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)

        svc = await MCPConnectionsService.get_instance()

        existing = await svc.get_server(server_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Server not found")

        update_data = body.model_dump(exclude_unset=True)
        existing = existing.model_copy(update=update_data)

        result = await svc.upsert_server(existing)

        track_event_if_configured(
            "MCP_Server_Updated",
            {"server_id": result.id, "server_name": result.server_name},
        )

        return {"server": result.model_dump(mode="json"), "updated": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating MCP server: {e}")
        raise HTTPException(status_code=500, detail="Failed to update MCP server")


@app_v4.delete("/mcp/connections/servers/{server_id}")
async def delete_mcp_server(server_id: str, request: Request):
    """Remove a server from the catalog."""
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        svc = await MCPConnectionsService.get_instance()
        deleted = await svc.delete_server(server_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Server not found")

        track_event_if_configured("MCP_Server_Deleted", {"server_id": server_id})
        return {"deleted": True, "server_id": server_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting MCP server: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete MCP server")


@app_v4.get("/mcp/connections/user")
async def get_user_mcp_connections(request: Request):
    """
    Get all MCP server connections for the authenticated user.

    Returns server catalog merged with user's connection status.
    """
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)

        svc = await MCPConnectionsService.get_instance()
        result = await svc.get_available_servers_for_user(user_id)

        return {"connections": result, "user_id": user_id}

    except Exception as e:
        logger.error(f"Error getting user connections: {e}")
        raise HTTPException(status_code=500, detail="Failed to get user connections")


@app_v4.get("/mcp/connections/user/{server_name}")
async def get_user_mcp_connection_by_server(server_name: str, request: Request):
    """
    Get a specific user's connection status for a given MCP server.

    Returns the connection object or 404 if no connection exists.
    """
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)

        svc = await MCPConnectionsService.get_instance()
        conn = await svc.get_user_connection(user_id, server_name)

        if not conn:
            raise HTTPException(
                status_code=404,
                detail=f"No connection found for server '{server_name}'",
            )

        return {"connection": conn.model_dump(mode="json"), "user_id": user_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting user connection: {e}")
        raise HTTPException(status_code=500, detail="Failed to get user connection")


async def _start_discovered_oauth(
    svc, server, user_id: str, resource_metadata_hint: Optional[str] = None
) -> str:
    """Zero-config OAuth for a URL the user pasted: discover the AS (RFC 9728 ->
    8414), register a client if none is cached (RFC 7591), mint PKCE, persist
    the pending context in Key Vault bound to the signed state, and promote the
    catalog entry to oauth2/oauth_refresh with the discovered endpoints. Returns
    the authorize URL for the popup. Raises HTTPException when the server
    exposes no standards-compliant metadata (then only a static token works).
    """
    from credential_resolver import CredentialResolver
    from v4.api.oauth_helpers import (
        build_authorize_url,
        dcr_provider_id,
        discover_oauth_metadata,
        dynamic_client_register,
        generate_pkce,
        sign_state,
        store_pending_oauth,
    )
    from v4.common.models.mcp_connection_models import (
        MCPAuthType,
        MCPCredentialSource,
    )

    resolver = CredentialResolver()

    # 1. Endpoints: reuse what a previous discovery persisted, else discover.
    auth_ep = server.oauth_authorize_url
    token_ep = server.oauth_token_url
    reg_ep = server.oauth_registration_url
    resource = server.oauth_resource
    scopes = list(server.oauth_scopes or [])
    if not (auth_ep and token_ep):
        meta = await discover_oauth_metadata(server.endpoint, resource_metadata_hint)
        if not meta:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"'{server.server_name}' requires authentication but exposes no "
                    "OAuth metadata (RFC 9728/8414). Provide a static token via "
                    "credentials instead."
                ),
            )
        auth_ep = meta["authorization_endpoint"]
        token_ep = meta["token_endpoint"]
        reg_ep = meta.get("registration_endpoint")
        resource = meta.get("resource") or server.endpoint
        if not scopes:
            scopes = list(meta.get("scopes_supported") or [])

    # 2. Client: cached dynamic registration in KV, else DCR now.
    client: Optional[dict] = None
    if server.oauth_client_ref:
        client = await resolver.resolve_credentials(
            "catalog", dcr_provider_id(server.server_name)
        )
    client_ref = server.oauth_client_ref
    if not client or not client.get("client_id"):
        if not reg_ep:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"'{server.server_name}' needs OAuth but its authorization server "
                    "offers no dynamic registration and no client is configured."
                ),
            )
        try:
            reg = await dynamic_client_register(
                reg_ep, client_name="MACAE", scopes=scopes or None
            )
        except Exception as exc:
            logger.error("DCR failed for %s: %s", server.server_name, exc)
            raise HTTPException(
                status_code=502, detail=f"Dynamic client registration failed: {exc}"
            )
        client = {
            "client_id": reg["client_id"],
            "client_secret": reg.get("client_secret") or "",
        }
        client_ref = await resolver.store_credentials(
            "catalog", dcr_provider_id(server.server_name), client
        )

    # 3. PKCE + state; secrets go to KV, only the HMAC state goes in the URL.
    verifier, challenge = generate_pkce()
    state = sign_state(user_id, server.server_name)
    await store_pending_oauth(
        resolver,
        user_id,
        server.server_name,
        state,
        {
            "code_verifier": verifier,
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret") or "",
            "token_endpoint": token_ep,
            "resource": resource or "",
            "scopes": " ".join(scopes),
        },
    )

    # 4. Promote the catalog entry so the UI and the resolver see the truth.
    changed = (
        server.auth_type != MCPAuthType.OAUTH2
        or server.credential_source != MCPCredentialSource.OAUTH_REFRESH
        or server.oauth_authorize_url != auth_ep
        or server.oauth_token_url != token_ep
        or server.oauth_registration_url != reg_ep
        or server.oauth_resource != resource
        or list(server.oauth_scopes or []) != scopes
        or server.oauth_client_ref != client_ref
    )
    if changed:
        server.auth_type = MCPAuthType.OAUTH2
        server.credential_source = MCPCredentialSource.OAUTH_REFRESH
        server.oauth_authorize_url = auth_ep
        server.oauth_token_url = token_ep
        server.oauth_registration_url = reg_ep
        server.oauth_resource = resource
        server.oauth_client_ref = client_ref
        server.oauth_scopes = scopes
        await svc.upsert_server(server)
        logger.info(
            "Catalog '%s' promoted to oauth2/oauth_refresh via discovery",
            server.server_name,
        )

    return build_authorize_url(
        auth_ep,
        client["client_id"],
        scopes,
        state,
        code_challenge=challenge,
        resource=resource,
    )


@app_v4.post("/mcp/connections/user/{server_name}/connect")
async def connect_user_to_mcp_server(server_name: str, request: Request):
    """
    Create a user connection entry for an MCP server.

    For servers with auth_type=none, immediately marks as active.
    For servers requiring auth:
    - If credentials provided in body, stores them in Key Vault and marks active
    - Otherwise, marks as pending_auth (OAuth flow)

    Request body (optional):
    {
      "credentials": { "access_token": "...", "api_key": "..." },
      "oauth_discovery": true,          # remote answered 401: discover+DCR+PKCE
      "resource_metadata": "https://..." # RFC 9728 hint from WWW-Authenticate
    }
    """
    try:
        from credential_resolver import CredentialResolver
        from v4.common.models.mcp_connection_models import (
            MCPConnectionStatus,
            MCPUserConnection,
        )
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)

        svc = await MCPConnectionsService.get_instance()

        # Verify server exists
        server = await svc.get_server_by_name(server_name)
        if not server:
            raise HTTPException(
                status_code=404, detail=f"Server '{server_name}' not found"
            )

        # Parse request body FIRST — oauth_discovery must be read before the
        # already_connected short-circuit, otherwise a re-call with
        # oauth_discovery=true after a 401 never runs discovery.
        body = {}
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            logger.debug(
                "connect_user_to_mcp_server: request body missing or not valid JSON; proceeding with empty body"
            )

        credentials = body.get("credentials")
        # ca-mcp sets this after the remote server answered 401 to an
        # unauthenticated connect: "this URL needs OAuth, discover it".
        oauth_discovery = bool(body.get("oauth_discovery"))
        resource_metadata_hint = body.get("resource_metadata")

        # Check existing connection — but NOT when oauth_discovery is requested:
        # the caller got a 401 from the remote, so the "active" status is stale
        # (registered as auth_type=none but the server actually requires OAuth).
        existing = await svc.get_user_connection(user_id, server_name)
        if (
            existing
            and existing.status == MCPConnectionStatus.ACTIVE
            and not oauth_discovery
        ):
            return {
                "connection": existing.model_dump(mode="json"),
                "already_connected": True,
            }

        # Determine status and secret_ref
        from v4.common.models.mcp_connection_models import (
            MCPAuthType,
            MCPCredentialSource,
        )

        status = MCPConnectionStatus.PENDING_AUTH
        secret_ref = None
        oauth_url: Optional[str] = None

        # Operator-preconfigured OAuth (client_id via env var) keeps its legacy path.
        _preconfigured_oauth = bool(
            server.auth_type == MCPAuthType.OAUTH2
            and server.oauth_authorize_url
            and server.oauth_client_id_env
            and server.oauth_client_secret_env
            and os.environ.get(server.oauth_client_id_env or "", "")
            and os.environ.get(server.oauth_client_secret_env or "", "")
        )
        # Discovery lane: explicitly requested (401 upstream) OR the entry says
        # oauth2 but nobody pre-registered a client for it.
        _needs_discovery = not credentials and (
            oauth_discovery
            or (server.auth_type == MCPAuthType.OAUTH2 and not _preconfigured_oauth)
        )

        if _needs_discovery:
            oauth_url = await _start_discovered_oauth(
                svc, server, user_id, resource_metadata_hint
            )
        elif server.auth_type == MCPAuthType.NONE:
            status = MCPConnectionStatus.ACTIVE
        elif server.credential_source == MCPCredentialSource.MANAGED_IDENTITY:
            # Managed Identity tokens are minted by the platform at call time;
            # no user credentials needed — mark active immediately.
            status = MCPConnectionStatus.ACTIVE
        elif credentials:
            try:
                resolver = CredentialResolver()
                secret_ref = await resolver.store_credentials(
                    user_id, server_name, credentials
                )
                status = MCPConnectionStatus.ACTIVE
                logger.info(
                    f"Stored credentials in Key Vault for user '{user_id}' "
                    f"connecting to '{server_name}'"
                )
            except Exception as kv_err:
                logger.error(f"Failed to store credentials in Key Vault: {kv_err}")
                raise HTTPException(
                    status_code=500,
                    detail="Failed to securely store credentials",
                )
        elif server.auth_type == MCPAuthType.OAUTH2:
            from v4.api.oauth_helpers import build_authorize_url, sign_state

            if not server.oauth_authorize_url:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Server '{server_name}' has auth_type=oauth2 but no "
                        f"oauth_authorize_url configured in catalog"
                    ),
                )

            client_id_env = server.oauth_client_id_env or ""
            client_id = os.environ.get(client_id_env, "") if client_id_env else ""
            if not client_id:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"OAuth client_id not configured for '{server_name}' "
                        f"(expected env var: {client_id_env or '<unset>'})"
                    ),
                )

            state = sign_state(user_id, server_name)
            oauth_url = build_authorize_url(
                server.oauth_authorize_url,
                client_id,
                server.oauth_scopes,
                state,
            )

        # Create or UPDATE the user's connection. A user has exactly ONE record
        # per server: when one already exists (e.g. active from a no-auth first
        # connect, then re-entered via the oauth_discovery lane after a 401) we
        # must update it in place — MCPUserConnection defaults `id` to a fresh
        # uuid, and Cosmos upsert keys on `id`, so a new object would INSERT a
        # duplicate. Duplicates break disconnect (only one gets deleted) and
        # make a re-login reuse the stale record instead of starting fresh.
        conn = MCPUserConnection(
            pk=user_id,
            user_id=user_id,
            server_id=server.id,
            server_name=server_name,
            status=status,
            secret_ref=secret_ref,
        )
        if existing:
            conn.id = existing.id
        result = await svc.upsert_user_connection(conn)

        track_event_if_configured(
            "MCP_User_Connected",
            {"user_id": user_id, "server_name": server_name, "status": status.value},
        )

        # `created` reflects reality: False when an existing (user, server)
        # record was updated in place (e.g. re-entered via oauth_discovery).
        response = {
            "connection": result.model_dump(mode="json"),
            "created": existing is None,
        }
        if oauth_url:
            response["oauth_url"] = oauth_url
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting user to MCP server: {e}")
        raise HTTPException(status_code=500, detail="Failed to connect to MCP server")


@app_v4.patch("/mcp/connections/user/{server_name}/activate")
async def activate_user_mcp_connection(server_name: str, request: Request):
    """
    Mark a user's MCP server connection as active.

    Called after OAuth callback completes successfully.
    Body (optional): { "secret_ref": "kv-secret-name" }
    """
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)

        body = {}
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            logger.debug(
                "activate_user_mcp_connection: request body missing or not valid JSON; proceeding with empty body"
            )

        svc = await MCPConnectionsService.get_instance()
        result = await svc.mark_connection_active(
            user_id, server_name, secret_ref=body.get("secret_ref")
        )

        track_event_if_configured(
            "MCP_User_Activated",
            {"user_id": user_id, "server_name": server_name},
        )

        return {"connection": result.model_dump(mode="json"), "activated": True}

    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error activating MCP connection: {e}")
        raise HTTPException(status_code=500, detail="Failed to activate connection")


@app_v4.get(
    "/mcp/connections/oauth/callback",
    responses={401: {"description": "Invalid or expired state token"}},
)
async def mcp_oauth_callback(query: Annotated[OAuthCallbackQuery, Query()]):
    """OAuth2 redirect callback.

    Verifies the signed state, exchanges the authorization code for a token,
    stores it in Key Vault, marks the user's connection as active, and returns
    an HTML page that closes the popup.
    """
    code, state = query.code, query.state
    from fastapi.responses import HTMLResponse

    from credential_resolver import CredentialResolver
    from v4.api.oauth_helpers import (
        exchange_code_for_token,
        load_pending_oauth,
        verify_state,
    )
    from v4.common.services.mcp_connections_service import MCPConnectionsService

    def _html(message: str, ok: bool = True, status_code: int = 200) -> HTMLResponse:
        color = "#0a7d3e" if ok else "#b3261e"
        return HTMLResponse(
            content=f"""<!doctype html>
<html><head><meta charset="utf-8"><title>OAuth</title></head>
<body style="font-family:system-ui;padding:32px;text-align:center;">
  <h2 style="color:{color};">{"Conexión exitosa" if ok else "Error en la conexión"}</h2>
  <p style="color:#555;">{message}</p>
  <script>
    try {{ if (window.opener) window.opener.postMessage(
        {{type: 'mcp_oauth', ok: {str(ok).lower()}}}, '*'); }} catch (e) {{}}
    setTimeout(() => window.close(), 1500);
  </script>
</body></html>""",
            status_code=status_code,
        )

    try:
        user_id, server_name = verify_state(state)
    except ValueError as ve:
        logger.warning(f"OAuth callback rejected invalid state: {ve}")
        # 401, not 400: the state is an auth artifact (signed token) and a
        # failed verification is an authorization failure, not a malformed
        # request — the request parsed fine, the credential in it did not.
        return _html(f"Token de estado inválido: {ve}", ok=False, status_code=401)

    svc = await MCPConnectionsService.get_instance()
    server = await svc.get_server_by_name(server_name)
    if not server:
        return _html(
            f"Servidor '{server_name}' no encontrado", ok=False, status_code=404
        )

    # Discovery lane: the pending ctx (code_verifier + dynamic client + token
    # endpoint) was stored in KV at /connect, bound to this state. Legacy lane:
    # operator env vars. Never mix.
    resolver = CredentialResolver()
    pending = await load_pending_oauth(resolver, user_id, server_name, state)
    code_verifier: Optional[str] = None
    resource: Optional[str] = None
    if pending:
        client_id = pending.get("client_id", "")
        client_secret = pending.get("client_secret") or ""
        token_url = pending.get("token_endpoint") or server.oauth_token_url
        code_verifier = pending.get("code_verifier")
        resource = pending.get("resource") or None
        scopes_used = [s for s in (pending.get("scopes") or "").split() if s]
        if not client_id or not token_url:
            return _html(
                "El contexto de autorización está incompleto.",
                ok=False,
                status_code=500,
            )
    else:
        client_id_env = server.oauth_client_id_env or ""
        client_secret_env = server.oauth_client_secret_env or ""
        client_id = os.environ.get(client_id_env, "") if client_id_env else ""
        client_secret = (
            os.environ.get(client_secret_env, "") if client_secret_env else ""
        )
        token_url = server.oauth_token_url
        scopes_used = list(server.oauth_scopes or [])
        if not client_id or not client_secret or not token_url:
            return _html(
                "OAuth no está completamente configurado en el catálogo.",
                ok=False,
                status_code=500,
            )

    try:
        token_data = await exchange_code_for_token(
            token_url,
            client_id,
            client_secret or None,
            code,
            code_verifier=code_verifier,
            resource=resource,
        )
    except Exception as exc:
        logger.error(f"OAuth token exchange failed for '{server_name}': {exc}")
        return _html(
            f"No se pudo intercambiar el código: {exc}", ok=False, status_code=502
        )

    # Enrich the raw provider response with everything credential_resolver needs to
    # REFRESH this token later from ca-mcp — which has neither the token endpoint nor
    # the OAuth client. Without these, oauth_refresh cannot mint a new access_token
    # when the current one expires. Providers rotate refresh_token, so the resolver
    # also writes the rotation back (needs KV Secrets Officer on its MI).
    import time as _time

    token_data = dict(token_data)
    token_data.setdefault("token_endpoint", token_url)
    token_data.setdefault("client_id", client_id)
    if client_secret:
        token_data.setdefault("client_secret", client_secret)
    if scopes_used:
        token_data.setdefault("scopes", scopes_used)
    if resource:
        token_data.setdefault("resource", resource)
    _expires_in = token_data.get("expires_in")
    if _expires_in and "expires_at" not in token_data:
        try:
            token_data["expires_at"] = str(int(_time.time()) + int(_expires_in))
        except (TypeError, ValueError):
            pass

    try:
        secret_ref = await resolver.store_credentials(user_id, server_name, token_data)
    except Exception as exc:
        logger.error(f"Failed to store OAuth token in Key Vault: {exc}")
        return _html(
            "No se pudo guardar el token de forma segura.",
            ok=False,
            status_code=500,
        )

    try:
        await svc.mark_connection_active(user_id, server_name, secret_ref=secret_ref)
    except Exception as exc:
        logger.error(f"Failed to mark connection active: {exc}")
        return _html(
            "El token se guardó pero no se pudo activar la conexión.",
            ok=False,
            status_code=500,
        )

    track_event_if_configured(
        "MCP_OAuth_Completed",
        {"user_id": user_id, "server_name": server_name},
    )

    return _html(f"Conectado a {server.display_name}. Puedes cerrar esta ventana.")


@app_v4.delete("/mcp/connections/user/{server_name}")
async def disconnect_user_from_mcp_server(server_name: str, request: Request):
    """Remove a user's connection to an MCP server."""
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)

        svc = await MCPConnectionsService.get_instance()
        deleted = await svc.disconnect_user(user_id, server_name)

        if not deleted:
            raise HTTPException(status_code=404, detail="Connection not found")

        track_event_if_configured(
            "MCP_User_Disconnected",
            {"user_id": user_id, "server_name": server_name},
        )

        return {"disconnected": True, "server_name": server_name}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error disconnecting from MCP server: {e}")
        raise HTTPException(status_code=500, detail="Failed to disconnect")


@app_v4.get("/mcp/inspector/status")
async def mcp_inspector_status():
    """Status of the MCP Inspector proxy (health + tokenized UI link).

    Existed before the router surgery (May logs show it answering 200) and the
    frontend still calls it (InspectorLink); the service layer survived intact,
    so this is a re-wire to MCPInspectorBridge, not a new subsystem.
    """
    try:
        from v4.common.services.mcp_inspector_bridge import get_inspector_bridge

        return await get_inspector_bridge().get_status()
    except Exception as e:
        logger.error(f"Error getting Inspector status: {e}")
        raise HTTPException(status_code=500, detail="Failed to get Inspector status")
