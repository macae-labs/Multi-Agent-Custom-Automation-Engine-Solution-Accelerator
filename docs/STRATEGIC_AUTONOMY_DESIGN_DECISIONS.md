# Strategic Autonomy - Design Clarifications

## Why Timer Trigger Instead of Service Bus Trigger?

### ❌ Service Bus Trigger Approach (NOT USED)
```
Service Bus → Azure Function → Direct action execution
```
**Problems:**
- Function needs to contain all business logic (tight coupling)
- No centralized orchestration
- Hard to test without Service Bus
- Difficult to add additional triggers (HTTP, manual, etc.)

### ✅ Timer Trigger Approach (IMPLEMENTED)
```
Timer → Function → HTTP POST → Backend API → Service Bus (if needed) → Consumer
```
**Benefits:**
- Function is lightweight (just HTTP call)
- Business logic centralized in backend
- API testable independently
- Multiple trigger types possible (Timer, manual HTTP call, webhook, etc.)
- Backend decides publication strategy

---

## Architecture Decision: Why Two-Stage Design?

### Stage 1: Timer → Backend API
**Timer Function**:
```python
async def strategic_autonomy_timer(timer: func.TimerRequest):
    # Lightweight: just call API
    response = await client.post("/api/strategic/analyze")
    log_status(response.json()["publication_status"])
```

**Backend API** (`POST /api/strategic/analyze`):
```python
async def trigger_strategic_analysis():
    # Heavy lifting here
    snapshot = await monitor.get_health_snapshot()
    trends = await store.get_trends()
    analysis = await agent.analyze_business_context(snapshot, trends)
    
    # Backend decides if should publish
    if health_score < 50:
        await publisher.publish_decision(...)
```

**Why?**
- Timer Function: Simple scheduler (1 responsibility)
- Backend API: Complex orchestration (analyzable, testable)
- Clear separation of concerns

---

### Stage 2: Service Bus → Consumer (Future)

When health is degraded, backend publishes to Service Bus:

```python
# In backend API
if health_score < 50:
    publisher.publish_decision(
        message_type="CRITICAL_FAILURE",
        health_snapshot=snapshot,
        analysis=analysis
    )
```

**Service Bus Consumer** (separate Azure Function):
```python
@app.service_bus_topic_trigger(
    topic_name="strategic-decisions",
    subscription_name="critical-failure-handler"
)
async def process_critical_failure(msg: ServiceBusMessage):
    payload = json.loads(msg.get_body().decode())
    
    # Execute autonomous actions
    for action in payload["analysis"]["recommended_actions"]:
        if action["can_execute_autonomously"]:
            await execute_action(action)
```

**Why separate consumer?**
- Decoupled: Failures in consumer don't affect monitoring
- Scalable: Consumer can scale independently
- Testable: Can publish test messages manually
- Multiple consumers: Different subscriptions for different severity levels

---

## Data Flow: Trends Handling

### ❌ WRONG: Agent calls HTTP internally
```python
class StrategicOrchestratorAgent:
    async def analyze_business_context(self, health_snapshot):
        # ❌ BAD: Agent coupled to HTTP
        response = await httpx.get("/api/observability/trends")
        trends = response.json()
        # analyze...
```

**Problems:**
- Agent coupled to HTTP layer
- Hard to test (needs running server)
- Circular dependency risk
- No control over caching/performance

### ✅ CORRECT: Backend passes trends as parameter
```python
# Backend orchestrates data gathering
async def trigger_strategic_analysis():
    snapshot = await monitor.get_health_snapshot()
    trends = await store.get_trends(days=7)  # Backend gets trends
    
    # Pass both to agent
    agent = await StrategicOrchestratorAgent.create()
    analysis = await agent.analyze_business_context(
        health_snapshot=snapshot,
        trends=trends,  # ✅ Passed as parameter
    )
```

**Benefits:**
- Agent is pure business logic (no HTTP dependencies)
- Testable: Just pass mock data
- Backend controls caching strategy
- Clear data flow: Backend → Agent → Decision

---

## Service Bus Publisher: Why Separate from Store?

### observability_snapshot_store.py
**Purpose**: Cosmos DB persistence and querying
```python
class ObservabilitySnapshotStore:
    async def persist_snapshot(snapshot: Dict) -> None:
        """Write to Cosmos"""
    
    async def get_trends(days: int) -> Dict:
        """Query Cosmos with aggregation"""
```

**Responsibility**: Storage layer (CRUD operations)

---

### service_bus_publisher.py
**Purpose**: Asynchronous messaging for orchestration
```python
class ServiceBusPublisher:
    async def publish_decision(
        message_type: str,
        health_snapshot: Dict,
        analysis: Dict
    ) -> bool:
        """Publish to Service Bus topic"""
```

**Responsibility**: Messaging layer (pub/sub)

---

### Why separate?

| Aspect | Snapshot Store | Service Bus Publisher |
|--------|----------------|----------------------|
| **Purpose** | Data persistence | Event notification |
| **Pattern** | Repository | Publisher |
| **Triggers** | Always (audit trail) | Conditional (only if needed) |
| **Consumers** | Backend queries | External consumers |
| **Failure mode** | Log warning, continue | Retry, then alert |
| **Testing** | Mock Cosmos | Mock Service Bus |

**Example usage**:
```python
# Both used independently in same endpoint
async def trigger_strategic_analysis():
    snapshot = await monitor.get_health_snapshot()
    
    # 1. Always persist (audit trail)
    await store.persist_snapshot(snapshot)  # ✅ Snapshot Store
    
    # 2. Conditionally publish (orchestration)
    if health_score < 50:
        await publisher.publish_decision(...)  # ✅ Service Bus Publisher
```

---

## Complete Flow with All Components

```
┌──────────────────────────────────────────────────────────────────┐
│                  SCHEDULED TRIGGER (External)                    │
│  Azure Functions Timer: runs every 15 min                        │
│  - Lightweight HTTP client                                       │
│  - No business logic                                             │
└──────────────────────────────────────────────────────────────────┘
                            │
                            │ POST /api/strategic/analyze
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                     BACKEND API (Orchestrator)                   │
│  app_kernel.py - POST /api/strategic/analyze                     │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ 1. Collect Health                                          │ │
│  │    AppHealthMonitor.get_health_snapshot()                  │ │
│  │    → provider_health + app_kpis                            │ │
│  └────────────────────────────────────────────────────────────┘ │
│                            │                                     │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ 2. Get Historical Trends (Backend retrieves)               │ │
│  │    ObservabilitySnapshotStore.get_trends(days=7)           │ │
│  │    → timeline, summary, anomalies                          │ │
│  └────────────────────────────────────────────────────────────┘ │
│                            │                                     │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ 3. Persist for Audit Trail                                 │ │
│  │    ObservabilitySnapshotStore.persist_snapshot()           │ │
│  │    → Cosmos DB document (best-effort)                      │ │
│  └────────────────────────────────────────────────────────────┘ │
│                            │                                     │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ 4. Strategic Analysis                                      │ │
│  │    agent = StrategicOrchestratorAgent.create()             │ │
│  │    analysis = agent.analyze_business_context(              │ │
│  │        health_snapshot=snapshot,  # From step 1            │ │
│  │        trends=trends              # From step 2            │ │
│  │    )                                                       │ │
│  │    → detected_issues, recommended_actions, autonomy_decision│ │
│  └────────────────────────────────────────────────────────────┘ │
│                            │                                     │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ 5. Conditional Service Bus Publication                     │ │
│  │    IF health_score < 50 OR force_publish:                  │ │
│  │        publisher = get_service_bus_publisher()             │ │
│  │        publisher.publish_decision(                         │ │
│  │            message_type=autonomy_decision.message_type,    │ │
│  │            health_snapshot=snapshot,                       │ │
│  │            analysis=analysis                               │ │
│  │        )                                                   │ │
│  └────────────────────────────────────────────────────────────┘ │
│                            │                                     │
│  Return: {                                                       │
│      health_snapshot, analysis, publication_status              │
│  }                                                               │
└──────────────────────────────────────────────────────────────────┘
                            │
         IF published to Service Bus
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│              SERVICE BUS TOPIC: strategic-decisions              │
│  Message: {                                                      │
│      type: "CRITICAL_FAILURE",                                   │
│      health_snapshot: {...},                                     │
│      analysis: {recommended_actions: [...]}                      │
│  }                                                               │
└──────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│         SERVICE BUS CONSUMER (Future Implementation)             │
│  Azure Function with Service Bus Trigger                         │
│                                                                  │
│  @service_bus_topic_trigger(topic="strategic-decisions")        │
│  async def process_strategic_decision(msg):                      │
│      actions = msg["analysis"]["recommended_actions"]            │
│      for action in actions:                                      │
│          if action["can_execute_autonomously"]:                  │
│              await execute_action(action)                        │
│          else:                                                   │
│              await alert_on_call_team(action)                    │
└──────────────────────────────────────────────────────────────────┘
```

---

## Key Design Principles

### 1. **Separation of Concerns**
- Timer: Scheduling only
- Backend API: Orchestration + business logic
- Service Bus: Async communication
- Consumer: Action execution

### 2. **No HTTP in Agent**
- Agent receives data as parameters
- Backend handles all HTTP/storage operations
- Agent is pure business logic

### 3. **Two Storage Systems**
- **Cosmos DB** (via snapshot_store): Historical data, trends, audit
- **Service Bus** (via publisher): Real-time events, orchestration

### 4. **Conditional Publishing**
- Not every health check publishes to Service Bus
- Only when `health_score < 50` or `force_publish=true`
- Reduces message volume and costs

### 5. **Graceful Degradation**
- If Cosmos fails: Log warning, analysis continues
- If Service Bus unavailable: MockPublisher used (dev mode)
- Backend always returns result, even if components fail

---

## Summary

✅ **Timer Trigger** → Lightweight scheduler, not business logic  
✅ **Backend API** → Orchestrates all data gathering and analysis  
✅ **Agent** → Pure business logic, no HTTP dependencies  
✅ **Service Bus Publisher** → Separate from store, handles messaging  
✅ **Future Consumer** → Executes actions based on Service Bus messages

This design is:
- **Testable**: Each component independently testable
- **Scalable**: Components scale independently
- **Maintainable**: Clear responsibilities
- **Observable**: Full audit trail in Cosmos
- **Cost-effective**: Conditional publishing reduces Service Bus messages
