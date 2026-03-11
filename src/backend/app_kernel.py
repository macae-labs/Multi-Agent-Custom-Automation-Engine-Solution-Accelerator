# app_kernel.py
import asyncio
import logging
import os
import uuid
from typing import Dict, List, Optional, cast, Any

# Semantic Kernel imports
from app_config import config
from auth.auth_utils import get_authenticated_user_details

# Azure monitoring
from azure.monitor.opentelemetry import configure_azure_monitor
from config_kernel import Config
from event_utils import track_event_if_configured
from tool_registry import ConnectToolResponse, ToolRegistry, ToolProvider, ToolDefinition, ConnectToolRequest
from credential_resolver import credential_resolver

# FastAPI imports
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from kernel_agents.agent_factory import AgentFactory
from kernel_agents.group_chat_manager import GroupChatManager
from kernel_agents.human_agent import HumanAgent

# Local imports
from middleware.health_check import HealthCheckMiddleware
from models.messages_kernel import (
    AgentMessage,
    AgentType,
    BaseDataModel,
    HumanClarification,
    HumanFeedback,
    InputTask,
    PlanWithSteps,
    Step,
)
from models.project_profile import CredentialBinding, ProjectProfile, ProjectProfileUpsert

from utils_kernel import initialize_runtime_and_context, rai_success

# Check if the Application Insights Instrumentation Key is set in the environment variables
connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip().strip('"').strip("'")
if connection_string:
    try:
        configure_azure_monitor(connection_string=connection_string)
        logging.info("Application Insights configured successfully")
    except Exception as exc:
        logging.warning(f"Application Insights configuration failed: {exc}")
else:
    logging.warning("Application Insights connection string not found. Telemetry disabled.")

# Configure logging
logging.basicConfig(level=logging.INFO)

# Suppress INFO logs from 'azure.core.pipeline.policies.http_logging_policy'
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(
    logging.WARNING
)
logging.getLogger("azure.identity.aio._internal").setLevel(logging.WARNING)

# Suppress verbose Cosmos DB logging
logging.getLogger("azure.cosmos").setLevel(logging.WARNING)
logging.getLogger("azure.cosmos._cosmos_http_logging_policy").setLevel(logging.WARNING)

# Suppress error logs from OpenTelemetry exporter
logging.getLogger("azure.monitor.opentelemetry.exporter.export._base").setLevel(
    logging.CRITICAL
)

# Initialize the FastAPI app
app = FastAPI()

frontend_url = Config.FRONTEND_SITE_NAME

# Add this near the top of your app.py, after initializing the app
app.add_middleware(
    CORSMiddleware,
    allow_origins=[frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure health check
app.add_middleware(HealthCheckMiddleware, password="", checks={})
logging.info("Added health check middleware")


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Close async clients to avoid unclosed aiohttp session warnings."""
    await credential_resolver.close()
    await config.close()


@app.on_event("startup")
async def startup_event() -> None:
    """Kick off background warmup for expensive auth-dependent clients."""
    await credential_resolver.initialize()


@app.post("/api/input_task")
async def input_task_endpoint(input_task: InputTask, request: Request):
    """
    Receive the initial input task from the user.
    """
    # Fix 1: Properly await the async rai_success function
    if not await rai_success(input_task.description):
        print("RAI failed")

        track_event_if_configured(
            "RAI failed",
            {
                "status": "Plan not created",
                "description": input_task.description,
                "session_id": input_task.session_id,
            },
        )

        return {
            "status": "Plan not created",
        }
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    if not user_id:
        track_event_if_configured(
            "UserIdNotFound", {"status_code": 400, "detail": "no user"}
        )
        raise HTTPException(status_code=400, detail="no user")

    # Generate session ID if not provided
    if not input_task.session_id:
        input_task.session_id = str(uuid.uuid4())

    try:
        # Create all agents instead of just the planner agent
        # This ensures other agents are created first and the planner has access to them
        kernel, memory_store = await initialize_runtime_and_context(
            input_task.session_id, user_id
        )
        client = None
        try:
            client = config.get_ai_project_client()
        except Exception as client_exc:
            logging.error(f"Error creating AIProjectClient: {client_exc}")

        agents = await AgentFactory.create_all_agents(
            session_id=input_task.session_id,
            user_id=user_id,
            memory_store=memory_store,
            client=client,
        )

        group_chat_manager = cast(
            GroupChatManager, agents[AgentType.GROUP_CHAT_MANAGER]
        )

        # Convert input task to JSON for the kernel function, add user_id here

        # Use the planner to handle the task
        await group_chat_manager.handle_input_task(input_task)

        # Get plan from memory store
        plan = await memory_store.get_plan_by_session(input_task.session_id)

        if not plan:  # If the plan is not found, raise an error
            track_event_if_configured(
                "PlanNotFound",
                {
                    "status": "Plan not found",
                    "session_id": input_task.session_id,
                    "description": input_task.description,
                },
            )
            raise HTTPException(status_code=404, detail="Plan not found")
        # Log custom event for successful input task processing
        track_event_if_configured(
            "InputTaskProcessed",
            {
                "status": f"Plan created with ID: {plan.id}",
                "session_id": input_task.session_id,
                "plan_id": plan.id,
                "description": input_task.description,
            },
        )
        return {
            "status": f"Plan created with ID: {plan.id}",
            "session_id": input_task.session_id,
            "plan_id": plan.id,
            "description": input_task.description,
        }

    except Exception as e:
        track_event_if_configured(
            "InputTaskError",
            {
                "session_id": input_task.session_id,
                "description": input_task.description,
                "error": str(e),
            },
        )
        raise HTTPException(status_code=400, detail=f"Error creating plan: {e}")


@app.post("/api/project_profile")
async def upsert_project_profile(
    profile_input: ProjectProfileUpsert, request: Request
):
    """Upsert project profile used for dynamic plugin injection."""
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    if not user_id:
        raise HTTPException(status_code=400, detail="no user")

    _, memory_store = await initialize_runtime_and_context(profile_input.session_id, user_id)

    # Add credential_bindings from profile_input if present, else default to None or []
    profile = ProjectProfile(
        data_type="project_profile",
        session_id=profile_input.session_id,
        user_id=user_id,
        project_id=profile_input.project_id,
        project_name=profile_input.project_name,
        api_base_url=profile_input.api_base_url,
        aws_s3_bucket=profile_input.aws_s3_bucket,
        firestore_root=profile_input.firestore_root,
        enabled_tools=profile_input.enabled_tools,
        api_key=profile_input.api_key,
        custom_config=profile_input.custom_config,
        credential_bindings=getattr(profile_input, "credential_bindings", []),
    )

    try:
        await memory_store.update_item(profile)
        AgentFactory.clear_cache(profile_input.session_id)
        return {
            "status": "Project profile saved",
            "session_id": profile_input.session_id,
            "project_id": profile_input.project_id,
            "enabled_tools": profile_input.enabled_tools,
            "credential_bindings": getattr(profile_input, "credential_bindings", None),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Error saving project profile: {exc}")


@app.get("/api/project_profile")
async def get_project_profile(session_id: str, request: Request):
    """Return project profile for a session, if it exists."""
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        raise HTTPException(status_code=400, detail="no user")

    _, memory_store = await initialize_runtime_and_context(session_id, user_id)
    records = await memory_store.get_data_by_type_and_session_id("project_profile", session_id)
    if not records:
        return {"project_profile": None}
    latest = records[-1]
    return {"project_profile": latest.model_dump()}


@app.post("/api/human_feedback")
async def human_feedback_endpoint(human_feedback: HumanFeedback, request: Request):
    """
    Receive human feedback on a step.

    ---
    tags:
      - Feedback
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
            step_id:
              type: string
              description: The ID of the step to provide feedback for
            plan_id:
              type: string
              description: The plan ID
            session_id:
              type: string
              description: The session ID
            approved:
              type: boolean
              description: Whether the step is approved
            human_feedback:
              type: string
              description: Optional feedback details
            updated_action:
              type: string
              description: Optional updated action
            user_id:
              type: string
              description: The user ID providing the feedback
    responses:
      200:
        description: Feedback received successfully
        schema:
          type: object
          properties:
            status:
              type: string
            session_id:
              type: string
            step_id:
              type: string
      400:
        description: Missing or invalid user information
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        track_event_if_configured(
            "UserIdNotFound", {"status_code": 400, "detail": "no user"}
        )
        raise HTTPException(status_code=400, detail="no user")

    kernel, memory_store = await initialize_runtime_and_context(
        human_feedback.session_id, user_id
    )

    client = None
    try:
        client = config.get_ai_project_client()
    except Exception as client_exc:
        logging.error(f"Error creating AIProjectClient: {client_exc}")

    human_agent = cast(
        HumanAgent,
        await AgentFactory.create_agent(
            agent_type=AgentType.HUMAN,
            session_id=human_feedback.session_id,
            user_id=user_id,
            memory_store=memory_store,
            client=client,
        ),
    )

    if human_agent is None:
        track_event_if_configured(
            "AgentNotFound",
            {
                "status": "Agent not found",
                "session_id": human_feedback.session_id,
                "step_id": human_feedback.step_id,
            },
        )
        raise HTTPException(status_code=404, detail="Agent not found")

    # Use the human agent to handle the feedback
    await human_agent.handle_human_feedback(human_feedback=human_feedback)

    track_event_if_configured(
        "Completed Feedback received",
        {
            "status": "Feedback received",
            "session_id": human_feedback.session_id,
            "step_id": human_feedback.step_id,
        },
    )
    return {
        "status": "Feedback received",
        "session_id": human_feedback.session_id,
        "step_id": human_feedback.step_id,
    }


@app.post("/api/human_clarification_on_plan")
async def human_clarification_endpoint(
    human_clarification: HumanClarification, request: Request
):
    """
    Receive human clarification on a plan.

    ---
    tags:
      - Clarification
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
            plan_id:
              type: string
              description: The plan ID requiring clarification
            session_id:
              type: string
              description: The session ID
            human_clarification:
              type: string
              description: Clarification details provided by the user
            user_id:
              type: string
              description: The user ID providing the clarification
    responses:
      200:
        description: Clarification received successfully
        schema:
          type: object
          properties:
            status:
              type: string
            session_id:
              type: string
      400:
        description: Missing or invalid user information
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        track_event_if_configured(
            "UserIdNotFound", {"status_code": 400, "detail": "no user"}
        )
        raise HTTPException(status_code=400, detail="no user")

    kernel, memory_store = await initialize_runtime_and_context(
        human_clarification.session_id, user_id
    )
    client = None
    try:
        client = config.get_ai_project_client()
    except Exception as client_exc:
        logging.error(f"Error creating AIProjectClient: {client_exc}")

    human_agent = cast(
        HumanAgent,
        await AgentFactory.create_agent(
            agent_type=AgentType.HUMAN,
            session_id=human_clarification.session_id,
            user_id=user_id,
            memory_store=memory_store,
            client=client,
        ),
    )

    if human_agent is None:
        track_event_if_configured(
            "AgentNotFound",
            {
                "status": "Agent not found",
                "session_id": human_clarification.session_id,
                "plan_id": human_clarification.plan_id,
            },
        )
        raise HTTPException(status_code=404, detail="Agent not found")

    # Store the human clarification message
    await human_agent.handle_human_clarification(
        human_clarification=human_clarification
    )

    plan = await memory_store.get_plan_by_session(human_clarification.session_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    agents = await AgentFactory.create_all_agents(
        session_id=human_clarification.session_id,
        user_id=user_id,
        memory_store=memory_store,
        client=client,
    )
    group_chat_manager = cast(
        GroupChatManager, agents[AgentType.GROUP_CHAT_MANAGER]
    )

    feedback = HumanFeedback(
        step_id=None,
        plan_id=plan.id,
        session_id=human_clarification.session_id,
        approved=True,
        human_feedback=human_clarification.human_clarification,
    )
    await group_chat_manager.handle_human_feedback(feedback)

    track_event_if_configured(
        "Completed Human clarification on the plan",
        {
            "status": "Clarification delegated to GroupChatManager",
            "session_id": human_clarification.session_id,
        },
    )
    return {
        "status": "Clarification delegated to GroupChatManager",
        "session_id": human_clarification.session_id,
    }


@app.post("/api/approve_step_or_steps")
async def approve_step_endpoint(
    human_feedback: HumanFeedback, request: Request
) -> Dict[str, str]:
    """
    Approve a step or multiple steps in a plan.

    ---
    tags:
      - Approval
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
            step_id:
              type: string
              description: Optional step ID to approve
            plan_id:
              type: string
              description: The plan ID
            session_id:
              type: string
              description: The session ID
            approved:
              type: boolean
              description: Whether the step(s) are approved
            human_feedback:
              type: string
              description: Optional feedback details
            updated_action:
              type: string
              description: Optional updated action
            user_id:
              type: string
              description: The user ID providing the approval
    responses:
      200:
        description: Approval status returned
        schema:
          type: object
          properties:
            status:
              type: string
      400:
        description: Missing or invalid user information
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        track_event_if_configured(
            "UserIdNotFound", {"status_code": 400, "detail": "no user"}
        )
        raise HTTPException(status_code=400, detail="no user")

    # Get the agents for this session
    kernel, memory_store = await initialize_runtime_and_context(
        human_feedback.session_id, user_id
    )
    client = None
    try:
        client = config.get_ai_project_client()
    except Exception as client_exc:
        logging.error(f"Error creating AIProjectClient: {client_exc}")
    agents = await AgentFactory.create_all_agents(
        session_id=human_feedback.session_id,
        user_id=user_id,
        memory_store=memory_store,
        client=client,
    )

    # Send the approval to the group chat manager
    group_chat_manager = cast(
        GroupChatManager, agents[AgentType.GROUP_CHAT_MANAGER]
    )

    await group_chat_manager.handle_human_feedback(human_feedback)

    # Return a status message
    if human_feedback.step_id:
        track_event_if_configured(
            "Completed Human clarification with step_id",
            {
                "status": f"Step {human_feedback.step_id} - Approval:{human_feedback.approved}."
            },
        )

        return {
            "status": f"Step {human_feedback.step_id} - Approval:{human_feedback.approved}."
        }
    else:
        track_event_if_configured(
            "Completed Human clarification without step_id",
            {"status": "All steps approved"},
        )

        return {"status": "All steps approved"}


@app.get("/api/plans")
async def get_plans(
    request: Request,
    session_id: Optional[str] = Query(None),
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
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        track_event_if_configured(
            "UserIdNotFound", {"status_code": 400, "detail": "no user"}
        )
        raise HTTPException(status_code=400, detail="no user")

    # Initialize memory context
    kernel, memory_store = await initialize_runtime_and_context(
        session_id or "", user_id
    )

    if session_id:
        plan = await memory_store.get_plan_by_session(session_id=session_id)
        if not plan:
            track_event_if_configured(
                "GetPlanBySessionNotFound",
                {"status_code": 400, "detail": "Plan not found"},
            )
            raise HTTPException(status_code=404, detail="Plan not found")

        # Use get_steps_by_plan to match the original implementation
        steps = await memory_store.get_steps_by_plan(plan_id=plan.id)
        plan_with_steps = PlanWithSteps(**plan.model_dump(), steps=steps)
        plan_with_steps.update_step_counts()
        return [plan_with_steps]
    if plan_id:
        plan = await memory_store.get_plan_by_plan_id(plan_id=plan_id)
        if not plan:
            track_event_if_configured(
                "GetPlanBySessionNotFound",
                {"status_code": 400, "detail": "Plan not found"},
            )
            raise HTTPException(status_code=404, detail="Plan not found")

        # Use get_steps_by_plan to match the original implementation
        steps = await memory_store.get_steps_by_plan(plan_id=plan.id)
        messages = await memory_store.get_data_by_type_and_session_id(
            "agent_message", session_id=plan.session_id
        )

        plan_with_steps = PlanWithSteps(**plan.model_dump(), steps=steps)
        plan_with_steps.update_step_counts()
        return [plan_with_steps, messages]

    all_plans = await memory_store.get_all_plans()
    # Fetch steps for all plans concurrently
    steps_for_all_plans = await asyncio.gather(
        *[memory_store.get_steps_by_plan(plan_id=plan.id) for plan in all_plans]
    )
    # Create list of PlanWithSteps and update step counts
    list_of_plans_with_steps = []
    for plan, steps in zip(all_plans, steps_for_all_plans):
        plan_with_steps = PlanWithSteps(**plan.model_dump(), steps=steps)
        plan_with_steps.update_step_counts()
        list_of_plans_with_steps.append(plan_with_steps)

    return list_of_plans_with_steps


@app.get("/api/steps/{plan_id}", response_model=List[Step])
async def get_steps_by_plan(plan_id: str, request: Request) -> List[Step]:
    """
    Retrieve steps for a specific plan.

    ---
    tags:
      - Steps
    parameters:
      - name: plan_id
        in: path
        type: string
        required: true
        description: The ID of the plan to retrieve steps for
    responses:
      200:
        description: List of steps associated with the specified plan
        schema:
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
              agent_reply:
                type: string
                description: Optional response from the agent after execution
              human_feedback:
                type: string
                description: Optional feedback provided by a human
              updated_action:
                type: string
                description: Optional modified action based on feedback
       400:
        description: Missing or invalid user information
      404:
        description: Plan or steps not found
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        track_event_if_configured(
            "UserIdNotFound", {"status_code": 400, "detail": "no user"}
        )
        raise HTTPException(status_code=400, detail="no user")

    # Initialize memory context
    kernel, memory_store = await initialize_runtime_and_context("", user_id)
    steps = await memory_store.get_steps_for_plan(plan_id=plan_id)
    return steps


@app.get("/api/agent_messages/{session_id}", response_model=List[AgentMessage])
async def get_agent_messages(session_id: str, request: Request) -> List[AgentMessage]:
    """
    Retrieve agent messages for a specific session.

    ---
    tags:
      - Agent Messages
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        in: path
        type: string
        required: true
        description: The ID of the session to retrieve agent messages for
    responses:
      200:
        description: List of agent messages associated with the specified session
        schema:
          type: array
          items:
            type: object
            properties:
              id:
                type: string
                description: Unique ID of the agent message
              session_id:
                type: string
                description: Session ID associated with the message
              plan_id:
                type: string
                description: Plan ID related to the agent message
              content:
                type: string
                description: Content of the message
              source:
                type: string
                description: Source of the message (e.g., agent type)
              timestamp:
                type: string
                format: date-time
                description: Timestamp of the message
              step_id:
                type: string
                description: Optional step ID associated with the message
      400:
        description: Missing or invalid user information
      404:
        description: Agent messages not found
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        track_event_if_configured(
            "UserIdNotFound", {"status_code": 400, "detail": "no user"}
        )
        raise HTTPException(status_code=400, detail="no user")

    # Initialize memory context
    kernel, memory_store = await initialize_runtime_and_context(
        session_id or "", user_id
    )
    data_items: List[BaseDataModel] = await memory_store.get_data_by_type(
        "agent_message"
    )
    return [item for item in data_items if isinstance(item, AgentMessage)]


@app.get("/api/agent_messages_by_plan/{plan_id}", response_model=List[AgentMessage])
async def get_agent_messages_by_plan(
    plan_id: str, request: Request
) -> List[AgentMessage]:
    """
    Retrieve agent messages for a specific session.

    ---
    tags:
      - Agent Messages
    parameters:
      - name: session_id
        in: path
        type: string
        required: true
        in: path
        type: string
        required: true
        description: The ID of the session to retrieve agent messages for
    responses:
      200:
        description: List of agent messages associated with the specified session
        schema:
          type: array
          items:
            type: object
            properties:
              id:
                type: string
                description: Unique ID of the agent message
              session_id:
                type: string
                description: Session ID associated with the message
              plan_id:
                type: string
                description: Plan ID related to the agent message
              content:
                type: string
                description: Content of the message
              source:
                type: string
                description: Source of the message (e.g., agent type)
              timestamp:
                type: string
                format: date-time
                description: Timestamp of the message
              step_id:
                type: string
                description: Optional step ID associated with the message
      400:
        description: Missing or invalid user information
      404:
        description: Agent messages not found
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        track_event_if_configured(
            "UserIdNotFound", {"status_code": 400, "detail": "no user"}
        )
        raise HTTPException(status_code=400, detail="no user")

    # Initialize memory context
    kernel, memory_store = await initialize_runtime_and_context("", user_id)
    data_items: List[BaseDataModel] = await memory_store.get_data_by_type(
        "agent_message"
    )
    return [
        item
        for item in data_items
        if isinstance(item, AgentMessage) and item.plan_id == plan_id
    ]


@app.delete("/api/messages")
async def delete_all_messages(request: Request) -> Dict[str, str]:
    """
    Delete all messages across sessions.

    ---
    tags:
      - Messages
    responses:
      200:
        description: Confirmation of deletion
        schema:
          type: object
          properties:
            status:
              type: string
              description: Status message indicating all messages were deleted
      400:
        description: Missing or invalid user information
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        track_event_if_configured(
            "UserIdNotFound", {"status_code": 400, "detail": "no user"}
        )
        raise HTTPException(status_code=400, detail="no user")

    # Initialize memory context
    kernel, memory_store = await initialize_runtime_and_context("", user_id)

    await memory_store.delete_all_items("plan")
    await memory_store.delete_all_items("session")
    await memory_store.delete_all_items("step")
    await memory_store.delete_all_items("agent_message")

    # Clear the agent factory cache
    AgentFactory.clear_cache()

    return {"status": "All messages deleted"}


@app.get("/api/messages")
async def get_all_messages(request: Request):
    """
    Retrieve all messages across sessions.

    ---
    tags:
      - Messages
    responses:
      200:
        description: List of all messages across sessions
        schema:
          type: array
          items:
            type: object
            properties:
              id:
                type: string
                description: Unique ID of the message
              data_type:
                type: string
                description: Type of the message (e.g., session, step, plan, agent_message)
              session_id:
                type: string
                description: Session ID associated with the message
              user_id:
                type: string
                description: User ID associated with the message
              content:
                type: string
                description: Content of the message
              timestamp:
                type: string
                format: date-time
                description: Timestamp of the message
      400:
        description: Missing or invalid user information
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    if not user_id:
        track_event_if_configured(
            "UserIdNotFound", {"status_code": 400, "detail": "no user"}
        )
        raise HTTPException(status_code=400, detail="no user")

    # Initialize memory context
    kernel, memory_store = await initialize_runtime_and_context("", user_id)
    message_list = await memory_store.get_all_items()
    return message_list


@app.get("/api/tools/providers")
async def get_tool_providers() -> List[ToolProvider]:
    """Get all available tool providers for onboarding."""
    return ToolRegistry.get_all_providers()


@app.get("/api/tools/agent/{agent_type}")
async def get_tools_for_agent(agent_type: str) -> List[ToolDefinition]:
    """Get all tools available for a specific agent."""
    return ToolRegistry.get_tools_for_agent(agent_type)


@app.post("/api/tools/connect")
async def connect_tool(request: ConnectToolRequest, http_request: Request) -> ConnectToolResponse:
    """Connect a tool by storing credentials in Key Vault."""
    provider = ToolRegistry.get_provider(request.provider_id)
    if not provider:
        raise HTTPException(
            status_code=404, detail=f"Provider {request.provider_id} not found"
        )

    required_fields = {field.name for field in provider.credential_fields if field.required}
    provided_fields = set(request.credentials.keys())
    missing_fields = required_fields - provided_fields

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required fields: {', '.join(missing_fields)}",
        )

    try:
        secret_uri = await credential_resolver.store_credentials(
            project_id=request.project_id,
            provider_id=request.provider_id,
            credentials=request.credentials,
        )

        # Store binding in Cosmos
        binding = CredentialBinding(
            provider_id=request.provider_id,
            secret_uri=secret_uri,
            is_active=True,
        )

        authenticated_user = get_authenticated_user_details(request_headers=http_request.headers)
        user_id = authenticated_user["user_principal_id"]
        if not user_id:
            raise HTTPException(status_code=400, detail="no user")

        _, memory_store = await initialize_runtime_and_context(request.session_id, user_id)
        profiles = await memory_store.get_data_by_type_and_session_id(
            "project_profile", request.session_id
        )
        if profiles:
            profile = cast(ProjectProfile, profiles[-1])
            existing_binding = next(
                (b for b in profile.credential_bindings if b.provider_id == request.provider_id),
                None,
            )
            if existing_binding:
                existing_binding.secret_uri = secret_uri
                existing_binding.is_active = True
            else:
                profile.credential_bindings.append(binding)
            await memory_store.update_item(profile)

        return ConnectToolResponse(
            success=True,
            secret_uri=secret_uri,
            message=f"Successfully connected {provider.display_name}",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store credentials: {str(e)}")


@app.get("/api/tools/credentials/{project_id}/{provider_id}")
async def get_credential_status(project_id: str, provider_id: str) -> Dict[str, Any]:
    """Check if credentials are configured for a project/provider."""
    credentials = await credential_resolver.resolve_credentials(project_id, provider_id)

    return {
        "project_id": project_id,
        "provider_id": provider_id,
        "is_configured": credentials is not None,
        "fields_configured": list(credentials.keys()) if credentials else []
    }


@app.delete("/api/tools/disconnect/{project_id}/{provider_id}")
async def disconnect_tool(project_id: str, provider_id: str) -> Dict[str, str]:
    """Disconnect a tool by removing credentials."""
    # TODO: Delete from Key Vault and Cosmos DB
    return {
        "status": "disconnected",
        "project_id": project_id,
        "provider_id": provider_id
    }


@app.get("/api/agent-tools")
async def get_agent_tools() -> List[Dict[str, Any]]:
    """
    Retrieve all available agent tools.

    ---
    tags:
      - Agent Tools
    responses:
      200:
        description: List of all available agent tools and their descriptions
        schema:
          type: array
          items:
            type: object
            properties:
              agent:
                type: string
                description: Name of the agent associated with the tool
              function:
                type: string
                description: Name of the tool function
              description:
                type: string
                description: Detailed description of what the tool does
              arguments:
                type: string
                description: Arguments required by the tool function
    """
    tools = ToolRegistry.get_all_tools()
    normalized = [
        {
            "agent": tool.agent_type,
            "function": tool.tool_id,
            "description": tool.description,
            "provider_id": tool.provider_id,
            "requires_credentials": tool.requires_credentials,
            "arguments": tool.parameters,
        }
        for tool in tools
    ]
    return sorted(normalized, key=lambda item: (item["agent"], item["function"]))


# Run the app
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app_kernel:app", host="127.0.0.1", port=8000, reload=True)
