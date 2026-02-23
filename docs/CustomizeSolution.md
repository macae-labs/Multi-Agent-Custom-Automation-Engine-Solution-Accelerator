# Table of Contents

- [Table of Contents](#table-of-contents)
- [Accelerating your own Multi-Agent - Custom Automation Engine MVP](#accelerating-your-own-multi-agent---custom-automation-engine-mvp)
  - [Technical Overview](#technical-overview)
  - [Architecture](#architecture)
    - [Key Technologies](#key-technologies)
  - [Adding a New Agent to the Multi-Agent System](#adding-a-new-agent-to-the-multi-agent-system)
    - [**Step 1: Define the New Agent's Tools**](#step-1-define-the-new-agents-tools)
    - [**Step 2: Create the Agent Class**](#step-2-create-the-agent-class)
    - [**Step 3: Register the Agent Type**](#step-3-register-the-agent-type)
    - [**Step 4: Register in Agent Factory**](#step-4-register-in-agent-factory)
    - [**Step 5: Update the Planner Agent**](#step-5-update-the-planner-agent)
    - [**Step 6: Update Frontend (Optional)**](#step-6-update-frontend-optional)
    - [**Step 7: Testing**](#step-7-testing)
  - [Implementing Real Tools with Connectors](#implementing-real-tools-with-connectors)
    - [Connector Architecture](#connector-architecture)
    - [Demo Mode vs Production Mode](#demo-mode-vs-production-mode)
    - [Creating a Custom Connector](#creating-a-custom-connector)
    - [API Reference](#api-reference)
    - [Models and Datatypes](#models-and-datatypes)
      - [Models](#models)
        - [**`BaseDataModel`**](#basedatamodel)
        - [**`AgentType`**](#agenttype)
        - [**`AgentMessage`**](#agentmessage)
        - [**`Session`**](#session)
        - [**`Plan`**](#plan)
        - [**`Step`**](#step)
        - [**`PlanWithSteps`**](#planwithsteps)
        - [**`InputTask`**](#inputtask)
        - [**`ApprovalRequest`**](#approvalrequest)
        - [**`HumanFeedback`**](#humanfeedback)
        - [**`HumanClarification`**](#humanclarification)
        - [**`ActionRequest`**](#actionrequest)
        - [**`ActionResponse`**](#actionresponse)
      - [Data Types](#data-types)
        - [**`DataType`**](#datatype)
        - [**`StepStatus`**](#stepstatus)
        - [**`PlanStatus`**](#planstatus)
        - [**`HumanFeedbackStatus`**](#humanfeedbackstatus)
    - [Application Flow](#application-flow)
      - [**Initialization**](#initialization)
      - [**Input Task Handling**](#input-task-handling)
      - [**Planning**](#planning)
      - [**Step Execution**](#step-execution)
  - [Agents Overview](#agents-overview)
    - [AgentFactory](#agentfactory)
    - [PlannerAgent](#planneragent)
    - [Specialized Agents](#specialized-agents)
  - [Persistent Storage with Cosmos DB](#persistent-storage-with-cosmos-db)
  - [Summary](#summary)
    - [Key Customization Points](#key-customization-points)


# Accelerating your own Multi-Agent - Custom Automation Engine MVP

As the name suggests, this project is designed to accelerate development of Multi-Agent solutions in your environment. The example solution presented shows how such a solution would be implemented and provides example agent definitions along with tools those agents use to accomplish tasks. You will want to implement real functions in your own environment, to be used by agents customized around your own use cases. Users can choose the LLM that is optimized for responsible use. The default LLM is GPT-4o which inherits the existing responsible AI mechanisms and filters from the LLM provider. We encourage developers to review [OpenAI's Usage policies](https://openai.com/policies/usage-policies/) and [Azure OpenAI's Code of Conduct](https://learn.microsoft.com/en-us/legal/cognitive-services/openai/code-of-conduct) when using GPT-4o. This document is designed to provide the in-depth technical information to allow you to add these customizations. Once the agents and tools have been developed, you will likely want to implement your own real world front end solution to replace the example in this accelerator.

## Technical Overview

This application is an AI-driven orchestration system built on **Semantic Kernel** that manages a group of AI agents to accomplish tasks based on user input. It uses a FastAPI backend to handle HTTP requests, processes them through various specialized agents, and stores stateful information using Azure Cosmos DB. The system is designed to:

- Receive input tasks from users.
- Generate a detailed plan to accomplish the task using a Planner agent.
- Execute the plan by delegating steps to specialized agents (e.g., HR, Procurement, Marketing, Tech Support).
- Incorporate human feedback into the workflow.
- Maintain state across sessions with persistent storage.
- Support both **demo mode** (simulated responses) and **production mode** (real integrations).

This code has not been tested as an end-to-end, reliable production application - it is a foundation to help accelerate building out multi-agent systems. You are encouraged to add your own data and functions to the agents, and then you must apply your own performance and safety evaluation testing frameworks to this system before deploying it.

Below, we'll dive into the details of each component, focusing on the endpoints, data types, and the flow of information through the system.

## Architecture

The solution uses **Semantic Kernel** with a clean separation of concerns:

```
src/backend/
├── kernel_agents/          # Agent definitions (Semantic Kernel agents)
│   ├── agent_factory.py    # Creates and configures agents
│   ├── planner_agent.py    # Plan generation agent
│   ├── hr_agent.py         # HR specialist agent
│   ├── marketing_agent.py  # Marketing specialist agent
│   ├── procurement_agent.py# Procurement specialist agent
│   ├── tech_support_agent.py # Tech support agent
│   └── generic_agent.py    # Base agent implementation
│
├── kernel_tools/           # Tools exposed to agents (@kernel_function)
│   ├── hr_tools.py         # HR-related functions
│   ├── marketing_tools.py  # Marketing functions
│   ├── procurement_tools.py# Procurement functions
│   └── tech_support_tools.py # Tech support functions
│
├── connectors/             # External service integrations
│   ├── base.py             # BaseConnector class and configuration
│   ├── graph_connector.py  # Microsoft Graph API integration
│   ├── database_connector.py # Employee/HR database operations
│   └── calendar_connector.py # Calendar scheduling
│
├── models/                 # Data models
│   └── messages.py         # AgentType, Step, Plan, etc.
│
└── app_kernel.py           # FastAPI application entry point
```

### Key Technologies

| Component | Technology |
|-----------|------------|
| Agent Framework | Semantic Kernel v1.32+ |
| Tool Definition | `@kernel_function` decorators |
| AI Service | Azure AI Agents SDK v1.2+ |
| Backend | FastAPI |
| Storage | Azure Cosmos DB |
| Connectors | Microsoft Graph, SQL Database |

## Adding a New Agent to the Multi-Agent System

This guide details the steps required to add a new agent to the Multi-Agent Custom Automation Engine. The process includes registering the agent, defining its capabilities through tools, and ensuring the PlannerAgent includes the new agent when generating activity plans.

### **Step 1: Define the New Agent's Tools**

Every agent is equipped with a set of tools (functions) that it can call to perform specific tasks. Tools are defined using Semantic Kernel's `@kernel_function` decorator.

1. **Create a New Tools File**: Create a file in `src/backend/kernel_tools/` for your agent's tools.

    Example (`kernel_tools/baker_tools.py`):
    ```python
    from semantic_kernel.functions import kernel_function
    from typing import Annotated
    from connectors.database_connector import get_database_connector

    class BakerTools:
        """Tools for the Baker Agent to handle baking tasks."""

        @staticmethod
        @kernel_function(
            name="bake_cookies",
            description="Bake cookies of a specific type and quantity."
        )
        async def bake_cookies(
            cookie_type: Annotated[str, "Type of cookies to bake (e.g., chocolate chip, oatmeal)"],
            quantity: Annotated[int, "Number of cookies to bake"]
        ) -> str:
            """Bake cookies and return confirmation."""
            db = get_database_connector()
            
            # In production, this would interact with inventory, scheduling, etc.
            result = await db.check_inventory(f"{cookie_type}_ingredients")
            
            demo_tag = "[DEMO] " if result.get("demo_mode") else ""
            
            return f"""##### {demo_tag}Cookies Baked Successfully
    **Type:** {cookie_type}
    **Quantity:** {quantity}
    **Status:** Ready for pickup ✓
    """

        @staticmethod
        @kernel_function(
            name="prepare_dough",
            description="Prepare dough of a specific type for baking."
        )
        async def prepare_dough(
            dough_type: Annotated[str, "Type of dough to prepare"]
        ) -> str:
            """Prepare dough and return confirmation."""
            return f"""##### Dough Prepared
    **Type:** {dough_type}
    **Status:** Ready for use ✓
    """
    ```

### **Step 2: Create the Agent Class**

Create a new agent file in `src/backend/kernel_agents/`.

Example (`kernel_agents/baker_agent.py`):
```python
from semantic_kernel import Kernel
from semantic_kernel.agents import ChatCompletionAgent
from semantic_kernel.connectors.ai import FunctionChoiceBehavior
from kernel_tools.baker_tools import BakerTools

BAKER_INSTRUCTIONS = """
You are an AI Agent specialized in baking tasks. You can:
- Bake various types of cookies
- Prepare different types of dough
- Manage baking schedules and inventory

Always confirm quantities and types before proceeding with baking tasks.
Provide clear status updates on all baking operations.
"""

def create_baker_agent(kernel: Kernel, agent_name: str = "BakerAgent") -> ChatCompletionAgent:
    """Create and configure the Baker Agent."""
    
    # Add tools to kernel
    kernel.add_plugin(BakerTools(), plugin_name="BakerTools")
    
    # Get the AI service
    service_id = kernel.get_service("agent_chat_service").service_id
    
    # Configure function calling behavior
    settings = kernel.get_prompt_execution_settings_from_service_id(service_id)
    settings.function_choice_behavior = FunctionChoiceBehavior.Auto()
    
    # Create the agent
    agent = ChatCompletionAgent(
        kernel=kernel,
        service_id=service_id,
        name=agent_name,
        instructions=BAKER_INSTRUCTIONS,
        execution_settings=settings
    )
    
    return agent
```

### **Step 3: Register the Agent Type**

Update `src/backend/models/messages.py` to include the new agent type:

```python
class AgentType(str, Enum):
    """Enum for agent types in the system."""
    planner = "PlannerAgent"
    hr_agent = "HrAgent"
    procurement_agent = "ProcurementAgent"
    marketing_agent = "MarketingAgent"
    tech_support_agent = "TechSupportAgent"
    generic_agent = "GenericAgent"
    baker_agent = "BakerAgent"  # Add new agent type
```

### **Step 4: Register in Agent Factory**

Update `src/backend/kernel_agents/agent_factory.py` to create your agent:

```python
from kernel_agents.baker_agent import create_baker_agent

class AgentFactory:
    """Factory for creating and managing agents."""
    
    @staticmethod
    def create_agent(agent_type: AgentType, kernel: Kernel) -> ChatCompletionAgent:
        """Create an agent based on the agent type."""
        
        if agent_type == AgentType.baker_agent:
            return create_baker_agent(kernel, "BakerAgent")
        # ... other agent types
```

### **Step 5: Update the Planner Agent**

Ensure the Planner Agent knows about the new agent's capabilities. Update `kernel_agents/planner_agent.py` to include the Baker Agent in the available agents list:

```python
AVAILABLE_AGENTS = """
- HrAgent: Handles HR tasks (onboarding, benefits, training)
- ProcurementAgent: Handles purchasing and vendor management
- MarketingAgent: Handles marketing campaigns and content
- TechSupportAgent: Handles IT support and technical issues
- BakerAgent: Handles baking tasks (cookies, dough preparation, baking schedules)
"""
```

### **Step 6: Update Frontend (Optional)**

Update `src/frontend/wwwroot/home/home.html` to add a quick task card:

```html
<div class="column">
    <div class="card is-hoverable quick-task">
        <div class="card-content">
            <i class="fa-solid fa-cookie has-text-warning mb-3"></i><br />
            <strong>Bake Cookies</strong>
            <p class="quick-task-prompt">Please bake 12 chocolate chip cookies for tomorrow's event.</p>
        </div>
    </div>
</div>
```

Update `src/frontend/wwwroot/task/task.js` to handle the agent icon:

```javascript
case "BakerAgent":
    agentIcon = "cookie";
    break;
```

### **Step 7: Testing**

Test your new agent:

```bash
# Start the backend
cd src/backend
python -m uvicorn app_kernel:app --host 0.0.0.0 --port 8000 --reload

# Test via API (session_id is required)
curl -X POST http://localhost:8000/api/input_task \
  -H "Content-Type: application/json" \
  -d '{"description": "Bake 24 chocolate chip cookies for the team meeting", "user_id": "test_user", "session_id": "test_session_001"}'

# Example: Test HR Agent onboarding
curl -X POST http://localhost:8000/api/input_task \
  -H "Content-Type: application/json" \
  -d '{"description": "Onboard new employee Maria Garcia to Engineering department", "user_id": "test_user", "session_id": "test_session_002"}'
```

## Implementing Real Tools with Connectors

The accelerator includes a **connector pattern** that allows tools to work in both demo mode (for testing) and production mode (with real integrations).

### Connector Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Kernel Tools                            │
│  (@kernel_function decorated methods)                       │
├─────────────────────────────────────────────────────────────┤
│                     Connectors                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │   Graph     │  │  Database   │  │     Calendar        │ │
│  │ Connector   │  │ Connector   │  │    Connector        │ │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘ │
├─────────┼────────────────┼───────────────────┼──────────────┤
│         │                │                   │              │
│    ┌────▼────┐      ┌────▼────┐        ┌────▼────┐        │
│    │Microsoft│      │   SQL   │        │ Outlook │        │
│    │  Graph  │      │   DB    │        │Calendar │        │
│    └─────────┘      └─────────┘        └─────────┘        │
└─────────────────────────────────────────────────────────────┘
```

### Demo Mode vs Production Mode

By default, the system runs in **demo mode**, returning simulated responses. To switch to production:

1. **Set Environment Variables**:
```bash
# Disable demo mode
export CONNECTOR_DEMO_MODE=false

# Microsoft Graph credentials (for email, calendar)
export MICROSOFT_GRAPH_CLIENT_ID=your-client-id
export MICROSOFT_GRAPH_CLIENT_SECRET=your-client-secret
export MICROSOFT_GRAPH_TENANT_ID=your-tenant-id

# Database connection (optional - for HR employee records)
export HR_DATABASE_URL=postgresql://user:pass@host:5432/hr_db
```

2. **Connector Configuration** (`connectors/base.py`):
```python
@dataclass
class ConnectorConfig:
    """Configuration for connectors loaded from environment."""
    demo_mode: bool = True
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_tenant_id: str = ""
    database_url: str = ""
    
    @classmethod
    def from_environment(cls) -> "ConnectorConfig":
        """Load configuration from environment variables."""
        return cls(
            demo_mode=os.getenv("CONNECTOR_DEMO_MODE", "true").lower() == "true",
            graph_client_id=os.getenv("MICROSOFT_GRAPH_CLIENT_ID", ""),
            graph_client_secret=os.getenv("MICROSOFT_GRAPH_CLIENT_SECRET", ""),
            graph_tenant_id=os.getenv("MICROSOFT_GRAPH_TENANT_ID", ""),
            database_url=os.getenv("HR_DATABASE_URL", ""),
        )
```

### Creating a Custom Connector

Example: Creating a Workday connector for HR integrations.

1. **Create the Connector** (`connectors/workday_connector.py`):
```python
from connectors.base import BaseConnector, ConnectorConfig
from typing import Dict, Any, Optional
import httpx

class WorkdayConnector(BaseConnector):
    """Connector for Workday HR system integration."""
    
    def __init__(self, config: Optional[ConnectorConfig] = None):
        super().__init__(config)
        self.base_url = os.getenv("WORKDAY_API_URL", "")
        self.api_key = os.getenv("WORKDAY_API_KEY", "")
    
    async def get_employee(self, employee_id: str) -> Dict[str, Any]:
        """Get employee information from Workday."""
        if self.is_demo_mode:
            return self._demo_response({
                "employee_id": employee_id,
                "name": "Demo Employee",
                "department": "Engineering",
                "start_date": "2024-01-15"
            })
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/workers/{employee_id}",
                headers={"Authorization": f"Bearer {self.api_key}"}
            )
            return response.json()
    
    async def create_onboarding_tasks(self, employee_id: str) -> Dict[str, Any]:
        """Create onboarding tasks in Workday."""
        if self.is_demo_mode:
            return self._demo_response({
                "tasks_created": 5,
                "employee_id": employee_id,
                "status": "pending"
            })
        
        # Production implementation
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/onboarding/tasks",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"worker_id": employee_id}
            )
            return response.json()

# Singleton pattern
_workday_connector: Optional[WorkdayConnector] = None

def get_workday_connector() -> WorkdayConnector:
    """Get or create the Workday connector singleton."""
    global _workday_connector
    if _workday_connector is None:
        _workday_connector = WorkdayConnector()
    return _workday_connector
```

2. **Use in Tools** (`kernel_tools/hr_tools.py`):
```python
from connectors.workday_connector import get_workday_connector

class HrTools:
    @staticmethod
    @kernel_function(
        name="get_employee_info",
        description="Get employee information from HR system."
    )
    async def get_employee_info(
        employee_id: Annotated[str, "The employee ID to look up"]
    ) -> str:
        workday = get_workday_connector()
        result = await workday.get_employee(employee_id)
        
        demo_tag = "[DEMO] " if result.get("demo_mode") else ""
        
        return f"""##### {demo_tag}Employee Information
**ID:** {result['employee_id']}
**Name:** {result['name']}
**Department:** {result['department']}
**Start Date:** {result['start_date']}
"""
```

### API Reference

To view the API reference, go to the API endpoint in a browser and add "/docs". This will bring up a full Swagger environment and reference documentation for the REST API included with this accelerator. For example, `https://macae-backend.eastus2.azurecontainerapps.io/docs`.  
If you prefer ReDoc, this is available by appending "/redoc".

![docs interface](./images/customize_solution/redoc_ui.png)

### Models and Datatypes

#### Models

##### **`BaseDataModel`**
The `BaseDataModel` is a foundational class for creating structured data models using Pydantic. It provides the following attributes:

- **`id`**: A unique identifier for the data, generated using `uuid`.
- **`ts`**: An optional timestamp indicating when the model instance was created or modified.

##### **`AgentType`**
The `AgentType` enumeration defines the types of agents in the system:

```python
class AgentType(str, Enum):
    planner = "PlannerAgent"
    hr_agent = "HrAgent"
    procurement_agent = "ProcurementAgent"
    marketing_agent = "MarketingAgent"
    tech_support_agent = "TechSupportAgent"
    generic_agent = "GenericAgent"
```

##### **`AgentMessage`**
The `AgentMessage` model represents communication between agents and includes the following fields:

- **`id`**: A unique identifier for the message, generated using `uuid`.
- **`data_type`**: A literal value of `"agent_message"` to identify the message type.
- **`session_id`**: The session associated with this message.
- **`user_id`**: The ID of the user associated with this message.
- **`plan_id`**: The ID of the related plan.
- **`content`**: The content of the message.
- **`source`**: The origin or sender of the message (e.g., an agent).
- **`ts`**: An optional timestamp for when the message was created.
- **`step_id`**: An optional ID of the step associated with this message.

##### **`Session`**
The `Session` model represents a user session and extends the `BaseDataModel`. It has the following attributes:

- **`data_type`**: A literal value of `"session"` to identify the type of data.
- **`current_status`**: The current status of the session (e.g., `active`, `completed`).
- **`message_to_user`**: An optional field to store any messages sent to the user.
- **`ts`**: An optional timestamp for the session's creation or last update.

##### **`Plan`**
The `Plan` model represents a high-level structure for organizing actions or tasks. It extends the `BaseDataModel` and includes the following attributes:

- **`data_type`**: A literal value of `"plan"` to identify the data type.
- **`session_id`**: The ID of the session associated with this plan.
- **`initial_goal`**: A description of the initial goal derived from the user's input.
- **`overall_status`**: The overall status of the plan (e.g., `in_progress`, `completed`, `failed`).

##### **`Step`**
The `Step` model represents a discrete action or task within a plan. It extends the `BaseDataModel` and includes the following attributes:

- **`data_type`**: A literal value of `"step"` to identify the data type.
- **`plan_id`**: The ID of the plan the step belongs to.
- **`action`**: The specific action or task to be performed.
- **`agent`**: The name of the agent responsible for executing the step.
- **`status`**: The status of the step (e.g., `planned`, `approved`, `completed`).
- **`agent_reply`**: An optional response from the agent after executing the step.
- **`human_feedback`**: Optional feedback provided by a user about the step.
- **`updated_action`**: Optional modified action based on human feedback.
- **`session_id`**: The session ID associated with the step.
- **`user_id`**: The ID of the user providing feedback or interacting with the step.

##### **`PlanWithSteps`**
The `PlanWithSteps` model extends the `Plan` model and includes additional information about the steps in the plan:

- **`steps`**: A list of `Step` objects associated with the plan.
- **`total_steps`**: The total number of steps in the plan.
- **`completed_steps`**: The number of steps that have been completed.
- **`pending_steps`**: The number of steps that are pending approval or completion.

##### **`InputTask`**
The `InputTask` model represents the user's initial input for creating a plan:

- **`session_id`**: An optional string for the session ID. If not provided, a new UUID will be generated.
- **`description`**: A string describing the task or goal the user wants to accomplish.
- **`user_id`**: The ID of the user providing the input.

##### **`ApprovalRequest`**
The `ApprovalRequest` model represents a request to approve a step or multiple steps:

- **`step_id`**: An optional string representing the specific step to approve.
- **`plan_id`**: The ID of the plan containing the step(s) to approve.
- **`session_id`**: The ID of the session associated with the approval request.
- **`approved`**: A boolean indicating whether the step(s) are approved.
- **`human_feedback`**: An optional string containing comments or feedback from the user.
- **`updated_action`**: An optional string representing a modified action based on feedback.
- **`user_id`**: The ID of the user making the approval request.

##### **`HumanFeedback`**
The `HumanFeedback` model captures user feedback on a specific step or plan:

- **`step_id`**: The ID of the step the feedback is related to.
- **`plan_id`**: The ID of the plan containing the step.
- **`session_id`**: The session ID associated with the feedback.
- **`approved`**: A boolean indicating if the step is approved.
- **`human_feedback`**: Optional comments or feedback provided by the user.
- **`updated_action`**: Optional modified action based on the feedback.
- **`user_id`**: The ID of the user providing the feedback.

##### **`HumanClarification`**
The `HumanClarification` model represents clarifications provided by the user about a plan:

- **`plan_id`**: The ID of the plan requiring clarification.
- **`session_id`**: The session ID associated with the plan.
- **`human_clarification`**: The clarification details provided by the user.
- **`user_id`**: The ID of the user providing the clarification.

##### **`ActionRequest`**
The `ActionRequest` model captures a request to perform an action within the system:

- **`session_id`**: The session ID associated with the action request.
- **`plan_id`**: The ID of the plan associated with the action.
- **`step_id`**: Optional ID of the step associated with the action.
- **`action`**: A string describing the action to be performed.
- **`user_id`**: The ID of the user requesting the action.

##### **`ActionResponse`**
The `ActionResponse` model represents the response to an action request:

- **`status`**: A string indicating the status of the action (e.g., `success`, `failure`).
- **`message`**: An optional string providing additional details or context about the action's result.
- **`data`**: Optional data payload containing any relevant information from the action.
- **`user_id`**: The ID of the user associated with the action response.

#### Data Types

##### **`DataType`**
The `DataType` enumeration defines the types of data used in the system:
- **`plan`**: Represents a plan data type.
- **`session`**: Represents a session data type.
- **`step`**: Represents a step data type.
- **`agent_message`**: Represents an agent message data type.

##### **`StepStatus`**
The `StepStatus` enumeration defines the possible statuses for a step:
- **`planned`**: Indicates the step is planned but not yet approved or completed.
- **`approved`**: Indicates the step has been approved.
- **`completed`**: Indicates the step has been completed.
- **`failed`**: Indicates the step has failed.

##### **`PlanStatus`**
The `PlanStatus` enumeration defines the possible statuses for a plan:
- **`in_progress`**: Indicates the plan is currently in progress.
- **`completed`**: Indicates the plan has been successfully completed.
- **`failed`**: Indicates the plan has failed.

##### **`HumanFeedbackStatus`**
The `HumanFeedbackStatus` enumeration defines the possible statuses for human feedback:
- **`pending`**: Indicates the feedback is awaiting review or action.
- **`addressed`**: Indicates the feedback has been addressed.
- **`rejected`**: Indicates the feedback has been rejected.

### Application Flow

#### **Initialization**

The initialization process sets up the necessary agents and context for a session using Semantic Kernel:

1. **Creating the Kernel**: A Semantic Kernel instance is created with Azure OpenAI services configured.
2. **Adding Plugins**: Tool classes are added as plugins to the kernel using `kernel.add_plugin()`.
3. **Creating Agents**: The `AgentFactory` creates specialized agents with their tools.
4. **Configuring Function Calling**: Agents are configured with `FunctionChoiceBehavior.Auto()` for automatic tool selection.

**Code Reference: `kernel_agents/agent_factory.py`**

```python
from semantic_kernel import Kernel
from semantic_kernel.agents import ChatCompletionAgent
from semantic_kernel.connectors.ai import FunctionChoiceBehavior

class AgentFactory:
    @staticmethod
    def create_agent(agent_type: AgentType, kernel: Kernel) -> ChatCompletionAgent:
        # Add plugins based on agent type
        if agent_type == AgentType.hr_agent:
            kernel.add_plugin(HrTools(), plugin_name="HrTools")
        
        # Configure settings
        settings = kernel.get_prompt_execution_settings_from_service_id(service_id)
        settings.function_choice_behavior = FunctionChoiceBehavior.Auto()
        
        # Create agent
        return ChatCompletionAgent(
            kernel=kernel,
            service_id=service_id,
            name=agent_type.value,
            instructions=AGENT_INSTRUCTIONS[agent_type],
            execution_settings=settings
        )
```

#### **Input Task Handling**

When the `/input_task` endpoint receives an `InputTask`:

1. Ensures a `session_id` is available (generates one if not provided).
2. Initializes the kernel and agents for the session.
3. Sends the task to the `PlannerAgent` for plan generation.
4. Returns the `session_id` and `plan_id`.

**Code Reference: `app_kernel.py`**

```python
@app.post("/input_task")
async def input_task(input_task: InputTask):
    session_id = input_task.session_id or str(uuid.uuid4())
    
    # Initialize kernel and create planner agent
    kernel = create_kernel()
    planner = AgentFactory.create_agent(AgentType.planner, kernel)
    
    # Generate plan
    plan = await planner.invoke(input_task.description)
    
    # Store in Cosmos DB
    await cosmos_memory.add_plan(plan)
    
    return {"status": "success", "session_id": session_id, "plan_id": plan.id}
```

#### **Planning**

The `PlannerAgent`:

1. Receives the task description.
2. Uses LLM with function calling to analyze the task.
3. Generates a `Plan` with detailed `Steps`, each assigned to a specialized agent.
4. Stores the plan and steps in Cosmos DB.

**Code Reference: `kernel_agents/planner_agent.py`**

#### **Step Execution**

For each step in the plan:

1. The appropriate agent is selected based on `step.agent`.
2. The agent invokes its tools using Semantic Kernel's function calling.
3. Results are stored and the step is marked complete.
4. Human feedback can be incorporated at any stage.

**Code Reference: `kernel_agents/generic_agent.py`**

## Agents Overview

### AgentFactory

**Role:** Creates and configures all agents in the system.  
**Location:** `kernel_agents/agent_factory.py`  
**Responsibilities:**

- Creates Semantic Kernel instances with appropriate services.
- Adds tool plugins to each agent's kernel.
- Configures function calling behavior.
- Returns fully configured `ChatCompletionAgent` instances.

### PlannerAgent

**Role:** Generates a detailed plan based on the input task.  
**Location:** `kernel_agents/planner_agent.py`  
**Responsibilities:**

- Parses the task description using LLM.
- Identifies which agents are needed.
- Creates a structured plan with specific actions.
- Handles re-planning if steps fail.

### Specialized Agents

**Types:** `HrAgent`, `ProcurementAgent`, `MarketingAgent`, `TechSupportAgent`  
**Role:** Execute specific actions related to their domain.  
**Responsibilities:**

- Receive action requests for their domain.
- Use `@kernel_function` tools to perform actions.
- Return structured results via connectors.
- Support both demo and production modes.

**Example:** `HrAgent` with its tools:
- `schedule_orientation_session` - Schedule new employee orientation
- `assign_mentor` - Assign a mentor to new employee
- `register_for_benefits` - Enroll in benefits programs
- `setup_payroll` - Configure payroll information
- `add_emergency_contact` - Add emergency contact info

![agent flow](./images/customize_solution/logic_flow.svg)

## Persistent Storage with Cosmos DB

The application uses Azure Cosmos DB to store and retrieve session data, plans, steps, and messages. This ensures that the state is maintained across different components and can handle multiple sessions concurrently.

**Key Points:**

- **Session Management:** Stores session information and current status.
- **Plan Storage:** Plans are saved and can be retrieved or updated.
- **Step Tracking:** Each step's status, actions, and feedback are stored.
- **Message History:** Chat messages between agents are stored for context.

**Cosmos DB Client Initialization:**

- Uses `DefaultAzureCredential` for authentication in production.
- Asynchronous operations are used throughout to prevent blocking.

**Code Reference: `cosmos_memory.py`**

**Data Modeling Best Practices:**

- Model data to minimize cross-partition queries.
- Use hierarchical partition keys for scalability.
- Partition by `session_id` for efficient queries.

## Summary

This application orchestrates a group of AI agents using **Semantic Kernel** to accomplish user-defined tasks by:

- Accepting tasks via HTTP endpoints.
- Generating detailed plans using LLM-powered agents.
- Delegating actions to specialized agents with real tool capabilities.
- Using the **connector pattern** for flexible demo/production modes.
- Incorporating human feedback at any stage.
- Maintaining state using Azure Cosmos DB.

### Key Customization Points

| Area | Location | Description |
|------|----------|-------------|
| Add new agent | `kernel_agents/` | Create agent class, register in factory |
| Add new tools | `kernel_tools/` | Use `@kernel_function` decorator |
| Add integrations | `connectors/` | Extend `BaseConnector` class |
| Modify planning | `planner_agent.py` | Update instructions and available agents |
| Change AI model | `app_config.py` | Update Azure OpenAI configuration |

For instructions to setup a local development environment for the solution, please see [deployment guide](./DeploymentGuide.md).
