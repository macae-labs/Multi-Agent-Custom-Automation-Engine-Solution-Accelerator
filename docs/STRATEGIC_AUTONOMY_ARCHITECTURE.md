# Strategic Autonomy with Azure Native Services

## Overview

This document describes how the **StrategicOrchestratorAgent** integrates with Azure native services to enable **autonomous self-healing** without Python loops or background workers.

The architecture follows the **Control Loop Pattern** using:
- **Azure Functions Timer Trigger** (orchestrator)
- **Cosmos DB Change Feed** (event detection)
- **Service Bus** (system nervous system)
- **Container Apps Health Probes** (availability monitoring)

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     STRATEGIC AUTONOMY LOOP (Azure Native)              │
└─────────────────────────────────────────────────────────────────────────┘

[Timer Trigger - Every 5-15 min]
         │
         ├─→ GET /api/observability/snapshot
         │        │
         │        ├─→ AppHealthMonitor.get_health_snapshot()
         │        ├─→ ObservabilitySnapshotStore.persist_snapshot() [Cosmos]
         │        └─→ Returns JSON with health_score, business_signals
         │
         └─→ [DECISION] health_score < 50%?
             │
             ├─ YES: health_score < 50%
             │    │
             │    ├─→ Cosmos Change Feed triggers
             │    │    (on health_score update)
             │    │
             │    └─→ Service Bus Publish:
             │         Topic: "strategic-decisions"
             │         Message: {"type": "CRITICAL_FAILURE", "score": health_score, ...}
             │
             └─ NO: health_score ≥ 50%
                  │
                  └─→ No escalation, continue monitoring

[Service Bus Consumer - StrategicOrchestratorAgent]
         │
         ├─→ Listen on Service Bus topic
         │
         └─→ On message received:
             │
             ├─→ Invoke analyze_business_context()
             ├─→ Get historical trends from /api/observability/trends
             ├─→ Generate autonomous actions
             │
             └─→ [Autonomy Decision]
                ├─ If can_execute_autonomously=True:
                │  └─→ Execute self-healing (no manual approval)
                │
                └─ If escalation=True:
                   └─→ Alert on-call team (page, Slack, email)
```

## Components

### 1. Azure Functions Timer Trigger

**Purpose**: Orchestrates the health check cycle at regular intervals.

**Trigger Configuration**:
```json
{
  "schedule": "0 */5 * * * *",
  "description": "Health snapshot collection every 5 minutes"
}
```

**Function Implementation** (pseudo-code):
```python
import azure.functions as func
import aiohttp
from datetime import datetime

async def health_orchestrator(timer: func.TimerRequest) -> None:
    """Collect health snapshot and publish to Service Bus if degraded."""
    
    # Call API endpoint
    async with aiohttp.ClientSession() as session:
        response = await session.get(
            "https://<app-service>.azurewebsites.net/api/observability/snapshot",
            headers={"Authorization": f"Bearer {token}"}
        )
        data = await response.json()
    
    health_score = data.get("health_score", 100)
    
    # If health degraded, publish to Service Bus
    if health_score < 50:
        service_bus_client.send_message(
            topic_name="strategic-decisions",
            message={
                "type": "CRITICAL_FAILURE",
                "health_score": health_score,
                "timestamp": datetime.utcnow().isoformat(),
                "snapshot": data,
            }
        )
        
        logging.info(f"Published CRITICAL alert: score={health_score}")
```

**Trigger Schedule Options**:
- `0 */5 * * * *` - Every 5 minutes (aggressive)
- `0 */15 * * * *` - Every 15 minutes (balanced)
- `0 0 * * * *` - Every hour (conservative)

### 2. Cosmos DB Change Feed

**Purpose**: Detects health degradation events in real-time.

**Configuration**:
```bicep
resource cosmosContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2023-04-15' = {
  name: '${cosmosDatabase.name}/snapshots'
  
  properties: {
    resource: {
      id: 'snapshots'
      partitionKey: {
        paths: ['/session_id']
      }
      changeFeedPolicy: {
        retentionInDays: 7  // Keep change feed for 7 days
      }
    }
  }
}
```

**Change Feed Trigger Function** (pseudo-code):
```python
@app.function_name("HealthChangeProcessor")
@app.cosmos_db_trigger(
    arg_name="items",
    database_name="fibroskin",
    collection_name="snapshots",
    connection_string_setting="CosmosDBConnectionString",
    lease_collection_name="leases",
    lease_connection_string_setting="CosmosDBConnectionString",
)
@app.service_bus_output(
    arg_name="msg",
    connection="ServiceBusConnection",
    queue_name="strategic-alerts"
)
async def process_health_change(items, msg):
    """React to health snapshot changes via Change Feed."""
    
    for item in items:
        health_score = item.get("health_score", 100)
        
        # If critical, immediately escalate
        if health_score < 30:
            msg.set(json.dumps({
                "severity": "CRITICAL",
                "health_score": health_score,
                "provider_health": item.get("provider_health", {}),
                "requires_immediate_action": True,
            }))
```

### 3. Service Bus Topic

**Purpose**: Decouples health detection from StrategicOrchestratorAgent execution.

**Bicep Configuration**:
```bicep
resource serviceBusNamespace 'Microsoft.ServiceBus/namespaces@2021-11-01' = {
  name: 'fibroskin-service-bus'
  location: location
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
}

resource strategicTopic 'Microsoft.ServiceBus/namespaces/topics@2021-11-01' = {
  name: 'strategic-decisions'
  parent: serviceBusNamespace
  properties: {
    defaultMessageTimeToLive: 'PT1H'  // 1 hour retention
    requiresDuplicateDetection: false
  }
}

resource criticalSubscription 'Microsoft.ServiceBus/namespaces/topics/subscriptions@2021-11-01' = {
  name: 'critical-failure-handler'
  parent: strategicTopic
  properties: {
    deadLetterOnMessageExpiration: true
    maxDeliveryCount: 3
  }
}
```

**Message Schema**:
```json
{
  "type": "CRITICAL_FAILURE | SELF_HEAL_ATTEMPT | MANUAL_REVIEW_REQUIRED",
  "health_score": 42.5,
  "timestamp": "2024-01-15T10:30:45Z",
  "detected_issues": [
    {
      "severity": "CRITICAL",
      "issue": "aws_s3 provider offline",
      "details": "Connection timeout after 30s"
    }
  ],
  "recommended_actions": [
    {
      "priority": "CRITICAL",
      "action": "Infrastructure Self-Healing",
      "can_execute_autonomously": true
    }
  ],
  "provider_health": {
    "firestore": { "is_healthy": true, "response_time_ms": 120 },
    "aws_s3": { "is_healthy": false, "response_time_ms": 0 }
  }
}
```

### 4. Container Apps Health Probes

**Purpose**: Continuously monitor application availability without a separate health check endpoint.

**Bicep Configuration**:
```bicep
resource containerApp 'Microsoft.App/containerApps@2023-04-01-preview' = {
  name: 'fibroskin-app'
  location: location
  
  properties: {
    template: {
      containers: [
        {
          name: 'fibroskin-api'
          image: '${containerRegistry}.azurecr.io/fibroskin:latest'
          
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
              initialDelaySeconds: 10
            },
            {
              type: 'Readiness'
              httpGet: {
                path: '/ready'
                port: 8000
              }
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 2
              initialDelaySeconds: 5
            },
            {
              type: 'Startup'
              httpGet: {
                path: '/startup'
                port: 8000
              }
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 10
              initialDelaySeconds: 0
            }
          ]
        }
      ]
    }
  }
}
```

**FastAPI Endpoints**:
```python
@app.get("/health")
async def health_check():
    """Liveness probe - returns 200 if container is alive."""
    monitor = AppHealthMonitor()
    snapshot = await monitor.get_health_snapshot()
    
    # Return 200 if system is responsive, 503 if degraded
    status_code = 200 if snapshot.health_score > 0 else 503
    
    return {
        "status": "healthy" if status_code == 200 else "degraded",
        "health_score": snapshot.health_score,
        "timestamp": datetime.utcnow().isoformat(),
    }, status_code

@app.get("/ready")
async def readiness_check():
    """Readiness probe - returns 200 if ready to accept requests."""
    monitor = AppHealthMonitor()
    snapshot = await monitor.get_health_snapshot()
    
    # Ready if health score >= 50%
    is_ready = snapshot.health_score >= 50
    status_code = 200 if is_ready else 503
    
    return {
        "ready": is_ready,
        "health_score": snapshot.health_score,
    }, status_code

@app.get("/startup")
async def startup_check():
    """Startup probe - returns 200 when app is fully initialized."""
    from app_kernel import app  # Ensure app is initialized
    
    return {"started": True}
```

## Complete Autonomous Flow

### Scenario: S3 Upload Failure

```
[T=0:00] Timer Trigger fires
    │
    ├─→ GET /api/observability/snapshot
    │    │
    │    └─→ AppHealthMonitor detects:
    │         - aws_s3: OFFLINE (timeout)
    │         - completion_rate: 65% (↓ from 95%)
    │         - error_rate: 12% (↑ from 2%)
    │         - health_score: 42%
    │
    └─→ health_score < 50% → PUBLISH to Service Bus

[T=0:01] Service Bus received CRITICAL_FAILURE
    │
    ├─→ Change Feed triggers (for Cosmos persistence)
    │    │
    │    └─→ Records historical trend point
    │
    └─→ StrategicOrchestratorAgent.analyze_business_context()
         │
         ├─→ Detects: "S3 provider offline + upload drop"
         │
         ├─→ Generates actions:
         │    1. CRITICAL: "Infrastructure Self-Healing" (autonomous)
         │    2. HIGH: "Rotate AWS credentials" (autonomous)
         │    3. MEDIUM: "Enable fallback storage" (manual)
         │
         └─→ Autonomy Decision:
             ├─ escalate=False (2 autonomous actions available)
             ├─ message_type="SELF_HEAL_ATTEMPT"
             │
             └─→ [EXECUTE]
                 ├─→ Step 1: Restart S3 connection pool
                 ├─→ Step 2: Verify IAM credentials
                 ├─→ Step 3: Run health check (should recover)
                 │
                 └─→ [VERIFY]
                    ├─ If health_score recovers to 80%:
                    │  └─→ Log: "Auto-recovery successful"
                    │
                    └─ If health_score stays < 50% after 30s:
                       └─→ Escalate to Service Bus (page on-call)

[T=0:05] Next Timer fires
    │
    └─→ GET /api/observability/snapshot
         │
         └─→ Verifies S3 is healthy again
              health_score: 88% ✓
              completion_rate: 92% ✓ (recovering)
              No new alerts
```

## Self-Healing Actions

The StrategicOrchestratorAgent can autonomously execute:

### 1. Infrastructure Self-Healing
```python
# In StrategicOrchestratorAgent.analyze_business_context()
if critical_provider_failure:
    action = {
        "action": "Infrastructure Self-Healing",
        "steps": [
            "Rotate credentials for failing provider",
            "Clear connection pools",
            "Clear caches",
            "Restart health checks",
        ],
        "can_execute_autonomously": True,
    }
```

### 2. Failover Activation
```python
# If aws_s3 is down and fallback exists
if provider_is_down("aws_s3") and has_fallback("gcs"):
    action = {
        "action": "Activate Fallback Storage",
        "description": "Route uploads to Google Cloud Storage",
        "steps": [
            "Enable GCS fallback flag",
            "Update adapter configuration",
            "Test with 5% traffic",
            "Monitor error rates",
        ],
        "can_execute_autonomously": True,
    }
```

### 3. Scaling Actions
```python
# If error rate spikes with high latency
if error_rate > 10 and latency_p99 > 5000:
    action = {
        "action": "Emergency Scaling",
        "description": "Increase Container App replicas",
        "steps": [
            "Scale from 3 to 10 replicas",
            "Monitor CPU/memory usage",
            "Drain slow requests",
        ],
        "can_execute_autonomously": True,
    }
```

## Monitoring & Observability

### View Health Timeline
```bash
curl https://<app-service>.azurewebsites.net/api/observability/trends?days=7 | jq '.timeline[]'
```

### View Current Health
```bash
curl https://<app-service>.azurewebsites.net/api/observability/snapshot | jq '.health_score'
```

### View Service Bus Messages
```bash
# In Azure Portal:
# Service Bus Namespace → Topics → strategic-decisions → Subscriptions → critical-failure-handler → Messages
```

## Cost Optimization

| Component | Cost | Notes |
|-----------|------|-------|
| Timer Trigger (every 5 min) | ~$0.50/month | 8,640 invocations/month |
| Service Bus (Standard) | ~$11.50/month | Includes 1M messages |
| Cosmos DB (RU-based) | ~$25-100/month | Depends on snapshot size & queries |
| Container App | ~$35-100/month | Depends on CPU/memory allocation |
| Change Feed storage | <$5/month | 7-day retention included |

**Total**: ~$72-242/month for full autonomous monitoring

## Deployment

### 1. Deploy Bicep Template
```bash
az deployment group create \
  --resource-group fibroskin-rg \
  --template-file infra/strategic_autonomy.bicep \
  --parameters infra/strategic_autonomy.parameters.json
```

### 2. Deploy Azure Functions
```bash
func azure functionapp publish fibroskin-functions \
  --build remote \
  --build-native-deps
```

### 3. Deploy Container App
```bash
az containerapp create \
  --resource-group fibroskin-rg \
  --environment fibroskin-env \
  --name fibroskin-app \
  --image fibroskin:latest
```

### 4. Configure Service Bus Binding
```bash
az functionapp config appsettings set \
  --name fibroskin-functions \
  --resource-group fibroskin-rg \
  --settings ServiceBusConnection="<connection-string>"
```

## Testing

### Simulate S3 Failure
```bash
# Mark aws_s3 as disabled in health monitor
export DISABLE_PROVIDERS="aws_s3"

# Call health endpoint
curl https://localhost:8000/health

# Should trigger StrategicOrchestratorAgent analysis
```

### Verify StrategicOrchestratorAgent Output
```bash
python src/backend/test_strategic_orchestrator.py
```

## Key Takeaways

✅ **No Python loops**: Uses Azure Functions Timer, not `while True`
✅ **Event-driven**: Cosmos Change Feed + Service Bus for real-time reactions
✅ **Autonomous**: Agent makes decisions without manual approval
✅ **Scalable**: Azure native services scale automatically
✅ **Observable**: Full audit trail in Cosmos DB
✅ **Cost-effective**: ~$100/month for full autonomy
✅ **Production-ready**: Uses managed Azure services with SLA guarantees
