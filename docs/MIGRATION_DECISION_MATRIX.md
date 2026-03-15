# Migration Decision Matrix: origin/main vs upstream/main

**Generated:** 2026-03-15
**Total Files with Differences:** 414
**Decision Legend:**
- **KEEP** = Fork customization, mantener
- **SYNC** = Restaurar desde upstream (archivo eliminado o modificado que debe alinearse)
- **REVIEW** = Requiere análisis manual antes de decidir

---

## SUMMARY BY CATEGORY (Conteos Reales del Diff)

| Category | Files | Status Type Breakdown |
|----------|-------|----------------------|
| .github/workflows | 24 | 22M, 1A, 1A |
| src/backend | 139 | 58A, 46D, 25M, 10R |
| src/frontend | 83 | 42M, 39D, 1A, 1R |
| src/mcp_server | 24 | 24D (todos eliminados) |
| src/tests | 50 | 50D (todos eliminados) |
| tests/e2e-test | 1 | 1M |
| infra | 39 | 37A, 2M |
| docs | 9 | 9A |
| src/agents | 11 | 11A |
| Root files | 26 | 14A, 8M, 4D |

**Leyenda Status:** A=Added, D=Deleted, M=Modified, R=Renamed

---

## 1. GITHUB WORKFLOWS (.github/workflows/) - 24 files

### Modified (22 files)
| File | Decision | Rationale |
|------|----------|-----------|
| `agnext-biab-02-containerimage.yml` | REVIEW | Comparar cambios vs upstream |
| `azure-dev.yml` | **KEEP** | OIDC auth customizations para macae-labs |
| `codeql.yml` | REVIEW | Security scanning, verificar diff |
| `create-release.yml` | **KEEP** | Usa codfish/semantic-release-action@v3, funcional |
| `deploy-orchestrator.yml` | REVIEW | Comparar cambios |
| `deploy-v2.yml` | **KEEP** | Pipeline custom con test integration |
| `deploy-waf.yml` | REVIEW | Verificar uso actual |
| `deploy.yml` | REVIEW | Verificar si reemplazado por deploy-v2 |
| `docker-build-and-push.yml` | **KEEP** | ACR integration para macae-labs |
| `job-cleanup-deployment.yml` | REVIEW | Comparar cambios |
| `job-deploy-linux.yml` | REVIEW | Comparar cambios |
| `job-deploy-windows.yml` | REVIEW | Comparar cambios |
| `job-deploy.yml` | **KEEP** | Core deployment job |
| `job-docker-build.yml` | REVIEW | Has customizations |
| `job-send-notification.yml` | REVIEW | Comparar cambios |
| `pr-title-checker.yml` | REVIEW | Standard PR checks |
| `pylint.yml` | REVIEW | Comparar cambios |
| `scheduled-Dependabot-PRs-Auto-Merge.yml` | REVIEW | Comparar cambios |
| `stale-bot.yml` | REVIEW | Standard issue management |
| `telemetry-template-check.yml` | REVIEW | Comparar cambios |
| `test-automation-v2.yml` | **KEEP** | E2E test pipeline custom |
| `test.yml` | **KEEP** | Backend tests con estructura macae |

### Added (2 files)
| File | Decision | Rationale |
|------|----------|-----------|
| `ca-macae-wsxymdijs3u2-AutoDeployTrigger-*.yml` | REVIEW | Auto-generated, verificar si necesario |
| `quality-gate.yml` | **KEEP** | Custom quality checks |

---

## 2. SRC/BACKEND - 139 files

### 2.1 Added Files - Custom Fork (58 files) - **KEEP**

#### Core Architecture (KEEP)
| File | Decision | Rationale |
|------|----------|-----------|
| `app_kernel.py` | **KEEP** | Entry point custom del fork |
| `app_config.py` | **KEEP** | Configuration loader custom |
| `config_kernel.py` | **KEEP** | Kernel configuration |
| `tool_registry.py` | **KEEP** | Tool registration |
| `credential_resolver.py` | **KEEP** | Credential management |
| `utils_kernel.py` | **KEEP** | Kernel utilities |
| `utils_date.py` | **KEEP** | Date utilities |
| `event_utils.py` | **KEEP** | Event utilities (renamed from common/) |
| `otlp_tracing.py` | **KEEP** | OTLP tracing (renamed from common/) |

#### Kernel Agents (KEEP)
| File | Decision | Rationale |
|------|----------|-----------|
| `kernel_agents/agent_base.py` | **KEEP** | Base agent implementation |
| `kernel_agents/agent_factory.py` | **KEEP** | Agent factory |
| `kernel_agents/agent_utils.py` | **KEEP** | Agent utilities |
| `kernel_agents/generic_agent.py` | **KEEP** | Generic agent |
| `kernel_agents/group_chat_manager.py` | **KEEP** | Group chat orchestration |
| `kernel_agents/hr_agent.py` | **KEEP** | HR agent |
| `kernel_agents/human_agent.py` | **KEEP** | Human agent |
| `kernel_agents/marketing_agent.py` | **KEEP** | Marketing agent |
| `kernel_agents/planner_agent.py` | **KEEP** | Planner agent |
| `kernel_agents/procurement_agent.py` | **KEEP** | Procurement agent |
| `kernel_agents/product_agent.py` | **KEEP** | Product agent |
| `kernel_agents/project_context_loader.py` | **KEEP** | Context loader |
| `kernel_agents/strategic_orchestrator_agent.py` | **KEEP** | Strategic orchestrator |
| `kernel_agents/tech_support_agent.py` | **KEEP** | Tech support agent |
| `kernel_agents/validator_agent.py` | **KEEP** | Validator agent |

#### Kernel Tools (KEEP)
| File | Decision | Rationale |
|------|----------|-----------|
| `kernel_tools/generic_tools.py` | **KEEP** | Generic tools |
| `kernel_tools/hr_tools.py` | **KEEP** | HR tools |
| `kernel_tools/marketing_tools.py` | **KEEP** | Marketing tools |
| `kernel_tools/procurement_tools.py` | **KEEP** | Procurement tools |
| `kernel_tools/product_tools.py` | **KEEP** | Product tools |
| `kernel_tools/tech_support_tools.py` | **KEEP** | Tech support tools |
| `kernel_tools/cloud_functions_plugin.py` | **KEEP** | Cloud functions integration |
| `kernel_tools/external_api_plugin.py` | **KEEP** | External API integration |
| `kernel_tools/firestore_plugin.py` | **KEEP** | Firestore integration |
| `kernel_tools/s3_plugin.py` | **KEEP** | S3 integration |

#### Adapters (KEEP)
| File | Decision | Rationale |
|------|----------|-----------|
| `adapters/base_adapter.py` | **KEEP** | Base adapter |
| `adapters/aws_adapter.py` | **KEEP** | AWS integration |
| `adapters/firestore_adapter.py` | **KEEP** | Firestore integration |
| `adapters/salesforce_adapter.py` | **KEEP** | Salesforce integration |

#### Connectors (KEEP)
| File | Decision | Rationale |
|------|----------|-----------|
| `connectors/__init__.py` | **KEEP** | Connectors init |
| `connectors/base.py` | **KEEP** | Base connector |
| `connectors/calendar_connector.py` | **KEEP** | Calendar integration |
| `connectors/database_connector.py` | **KEEP** | Database connector |
| `connectors/graph_connector.py` | **KEEP** | Microsoft Graph integration |
| `connectors/smtp_connector.py` | **KEEP** | SMTP integration |

#### Context & Models (KEEP)
| File | Decision | Rationale |
|------|----------|-----------|
| `context/__init__.py` | **KEEP** | Context init |
| `context/cosmos_memory_kernel.py` | **KEEP** | CosmosDB memory |
| `models/__init__.py` | **KEEP** | Models init |
| `models/messages_kernel.py` | **KEEP** | Message models |
| `models/project_profile.py` | **KEEP** | Project profile model |

#### Handlers (KEEP)
| File | Decision | Rationale |
|------|----------|-----------|
| `handlers/__init__.py` | **KEEP** | Handlers init |
| `handlers/runtime_interrupt_kernel.py` | **KEEP** | Runtime interrupt |

#### Observability (KEEP)
| File | Decision | Rationale |
|------|----------|-----------|
| `observability/__init__.py` | **KEEP** | Observability init |
| `observability/app_health_monitor.py` | **KEEP** | Health monitoring |
| `observability/context_injector.py` | **KEEP** | Context injection |
| `observability/firestore_health_checker.py` | **KEEP** | Firestore health |
| `observability/observability_snapshot_store.py` | **KEEP** | Snapshot store |
| `observability/provider_health_checker.py` | **KEEP** | Provider health |
| `observability/s3_health_checker.py` | **KEEP** | S3 health |
| `observability/service_bus_publisher.py` | **KEEP** | Service bus |

#### Utils (KEEP)
| File | Decision | Rationale |
|------|----------|-----------|
| `utils/pii_redactor.py` | **KEEP** | PII redaction |

#### Azure Functions (KEEP)
| File | Decision | Rationale |
|------|----------|-----------|
| `azure_functions/.funcignore` | **KEEP** | Functions config |
| `azure_functions/host.json` | **KEEP** | Functions host |
| `azure_functions/strategic_autonomy_timer/*` | **KEEP** | Strategic timer function |
| `azure_functions/strategic_decision_consumer/*` | **KEEP** | Strategic consumer function |

#### Tests Added (KEEP)
| File | Decision | Rationale |
|------|----------|-----------|
| `tests/conftest.py` | **KEEP** | Test configuration |
| `tests/context/__init__.py` | **KEEP** | Context tests init |
| `tests/context/test_cosmos_memory.py` | **KEEP** | CosmosDB tests |
| `tests/handlers/__init__.py` | **KEEP** | Handlers tests init |
| `tests/test_agent_integration.py` | **KEEP** | Agent integration tests |
| `tests/test_group_chat_manager_integration.py` | **KEEP** | Group chat tests |
| `tests/test_hr_agent_integration.py` | **KEEP** | HR agent tests |
| `tests/test_human_agent_integration.py` | **KEEP** | Human agent tests |
| `tests/test_multiple_agents_integration.py` | **KEEP** | Multiple agents tests |
| `tests/test_pii_redactor.py` | **KEEP** | PII redactor tests |
| `tests/test_planner_agent_integration.py` | **KEEP** | Planner tests |
| `tests/test_validator_agent.py` | **KEEP** | Validator tests |

#### Review Files
| File | Decision | Rationale |
|------|----------|-----------|
| `delete_tech_agent.py` | REVIEW | Verificar si es script temporal |
| `test_observability.py` | REVIEW | Mover a tests/ si útil |
| `test_strategic_orchestrator.py` | REVIEW | Mover a tests/ si útil |
| `validate_observability.py` | REVIEW | Verificar utilidad |

### 2.2 Deleted Files - Upstream Structure (46 files) - **SYNC**

Estos archivos existen en upstream pero fueron eliminados del fork. Decisión: **SYNC** para restaurarlos si se necesita compatibilidad con upstream.

| File | Decision | Rationale |
|------|----------|-----------|
| `app.py` | **SYNC** | Restaurar por compatibilidad con upstream (no reemplaza `app_kernel.py` como entry point principal del fork) |
| `common/__init__.py` | **SYNC** | Upstream common module |
| `common/config/__init__.py` | **SYNC** | Upstream config |
| `common/config/app_config.py` | **SYNC** | Upstream config |
| `common/database/__init__.py` | **SYNC** | Upstream database |
| `common/database/cosmosdb.py` | **SYNC** | Upstream CosmosDB |
| `common/database/database_base.py` | **SYNC** | Upstream database base |
| `common/database/database_factory.py` | **SYNC** | Upstream database factory |
| `common/models/__init__.py` | **SYNC** | Upstream models |
| `common/models/messages_af.py` | **SYNC** | Upstream messages |
| `common/utils/utils_af.py` | **SYNC** | Upstream utils |
| `common/utils/utils_agents.py` | **SYNC** | Upstream utils |
| `common/utils/utils_date.py` | **SYNC** | Upstream utils |
| `v4/api/router.py` | **SYNC** | Upstream v4 API |
| `v4/callbacks/__init__.py` | **SYNC** | Upstream callbacks |
| `v4/callbacks/global_debug.py` | **SYNC** | Upstream callbacks |
| `v4/callbacks/response_handlers.py` | **SYNC** | Upstream callbacks |
| `v4/common/services/__init__.py` | **SYNC** | Upstream services |
| `v4/common/services/agents_service.py` | **SYNC** | Upstream services |
| `v4/common/services/base_api_service.py` | **SYNC** | Upstream services |
| `v4/common/services/foundry_service.py` | **SYNC** | Upstream services |
| `v4/common/services/mcp_service.py` | **SYNC** | Upstream services |
| `v4/common/services/plan_service.py` | **SYNC** | Upstream services |
| `v4/common/services/team_service.py` | **SYNC** | Upstream services |
| `v4/config/__init__.py` | **SYNC** | Upstream config |
| `v4/config/agent_registry.py` | **SYNC** | Upstream registry |
| `v4/config/settings.py` | **SYNC** | Upstream settings |
| `v4/magentic_agents/common/lifecycle.py` | **SYNC** | Upstream lifecycle |
| `v4/magentic_agents/foundry_agent.py` | **SYNC** | Upstream foundry agent |
| `v4/magentic_agents/magentic_agent_factory.py` | **SYNC** | Upstream factory |
| `v4/magentic_agents/models/agent_models.py` | **SYNC** | Upstream models |
| `v4/magentic_agents/proxy_agent.py` | **SYNC** | Upstream proxy agent |
| `v4/models/messages.py` | **SYNC** | Upstream messages |
| `v4/models/models.py` | **SYNC** | Upstream models |
| `v4/models/orchestration_models.py` | **SYNC** | Upstream orchestration |
| `v4/orchestration/__init__.py` | **SYNC** | Upstream orchestration |
| `v4/orchestration/helper/plan_to_mplan_converter.py` | **SYNC** | Upstream converter |
| `v4/orchestration/human_approval_manager.py` | **SYNC** | Upstream approval |
| `v4/orchestration/orchestration_manager.py` | **SYNC** | Upstream orchestration |
| `.dockerignore` | **SYNC** | Upstream dockerignore |
| `Dockerfile.NoCache` | REVIEW | Verificar si necesario |
| `tests/test_team_specific_methods.py` | **SYNC** | Upstream test |

### 2.3 Modified Files (25 files)
| File | Decision | Rationale |
|------|----------|-----------|
| `.env.sample` | REVIEW | Comparar variables |
| `Dockerfile` | REVIEW | Comparar cambios |
| `README.md` | REVIEW | Comparar contenido |
| `__init__.py` | REVIEW | Comparar cambios |
| `pyproject.toml` | REVIEW | Comparar dependencias |
| `requirements.txt` | **KEEP** | PR #33 actualiza SDK versions |
| `uv.lock` | REVIEW | Regenerar después de sync |
| `tests/auth/test_auth_utils.py` | REVIEW | Comparar tests |
| `tests/middleware/test_health_check.py` | REVIEW | Comparar tests |
| `tests/models/test_messages.py` | REVIEW | Comparar tests |
| `tests/test_app.py` | REVIEW | Comparar tests |
| `tests/test_config.py` | REVIEW | Comparar tests |
| `tests/test_otlp_tracing.py` | REVIEW | Comparar tests |

---

## 3. SRC/MCP_SERVER - 24 files (ALL DELETED)

**Status:** Todos los 24 archivos fueron eliminados del fork pero existen en upstream.
**Decision:** **SYNC** - Restaurar desde upstream.

| File | Decision |
|------|----------|
| `.env.example` | **SYNC** |
| `Dockerfile` | **SYNC** |
| `README.md` | **SYNC** |
| `README_NEW.md` | **SYNC** |
| `__init__.py` | **SYNC** |
| `config/__init__.py` | **SYNC** |
| `config/settings.py` | **SYNC** |
| `core/__init__.py` | **SYNC** |
| `core/factory.py` | **SYNC** |
| `docker-compose.yml` | **SYNC** |
| `mcp_server.py` | **SYNC** |
| `pyproject.toml` | **SYNC** |
| `pytest.ini` | **SYNC** |
| `services/__init__.py` | **SYNC** |
| `services/data_tool_service.py` | **SYNC** |
| `services/general_service.py` | **SYNC** |
| `services/hr_service.py` | **SYNC** |
| `services/marketing_service.py` | **SYNC** |
| `services/product_service.py` | **SYNC** |
| `services/tech_support_service.py` | **SYNC** |
| `utils/__init__.py` | **SYNC** |
| `utils/date_utils.py` | **SYNC** |
| `utils/formatters.py` | **SYNC** |
| `uv.lock` | **SYNC** |

**Comando para restaurar:**
```bash
git checkout upstream/main -- src/mcp_server/
```

---

## 4. SRC/TESTS - 50 files (ALL DELETED)

**Status:** Todos los 50 archivos fueron eliminados del fork pero existen en upstream.
**Decision:** **SYNC** - Restaurar desde upstream.

### agents/ (7 files)
| File | Decision |
|------|----------|
| `agents/__init__py` | **SYNC** |
| `agents/interactive_test_harness/foundry_agent_interactive.py` | **SYNC** |
| `agents/interactive_test_harness/reasoning_agent_interactive.py` | **SYNC** |
| `agents/test_foundry_integration.py` | **SYNC** |
| `agents/test_human_approval_manager.py` | **SYNC** |
| `agents/test_proxy_agent.py` | **SYNC** |
| `agents/test_reasoning_agent.py` | **SYNC** |

### backend/ (38 files)
| File | Decision |
|------|----------|
| `backend/auth/__init__.py` | **SYNC** |
| `backend/auth/conftest.py` | **SYNC** |
| `backend/auth/test_auth_utils.py` | **SYNC** |
| `backend/common/config/__init__.py` | **SYNC** |
| `backend/common/config/test_app_config.py` | **SYNC** |
| `backend/common/database/__init__.py` | **SYNC** |
| `backend/common/database/test_cosmosdb.py` | **SYNC** |
| `backend/common/database/test_database_base.py` | **SYNC** |
| `backend/common/database/test_database_factory.py` | **SYNC** |
| `backend/common/utils/test_event_utils.py` | **SYNC** |
| `backend/common/utils/test_otlp_tracing.py` | **SYNC** |
| `backend/common/utils/test_utils_af.py` | **SYNC** |
| `backend/common/utils/test_utils_agents.py` | **SYNC** |
| `backend/common/utils/test_utils_date.py` | **SYNC** |
| `backend/middleware/test_health_check.py` | **SYNC** |
| `backend/test_app.py` | **SYNC** |
| `backend/v4/api/test_router.py` | **SYNC** |
| `backend/v4/callbacks/test_global_debug.py` | **SYNC** |
| `backend/v4/callbacks/test_response_handlers.py` | **SYNC** |
| `backend/v4/common/services/test_agents_service.py` | **SYNC** |
| `backend/v4/common/services/test_base_api_service.py` | **SYNC** |
| `backend/v4/common/services/test_foundry_service.py` | **SYNC** |
| `backend/v4/common/services/test_mcp_service.py` | **SYNC** |
| `backend/v4/common/services/test_plan_service.py` | **SYNC** |
| `backend/v4/common/services/test_team_service.py` | **SYNC** |
| `backend/v4/config/test_agent_registry.py` | **SYNC** |
| `backend/v4/config/test_settings.py` | **SYNC** |
| `backend/v4/magentic_agents/__init__.py` | **SYNC** |
| `backend/v4/magentic_agents/common/test_lifecycle.py` | **SYNC** |
| `backend/v4/magentic_agents/models/__init__.py` | **SYNC** |
| `backend/v4/magentic_agents/models/test_agent_models.py` | **SYNC** |
| `backend/v4/magentic_agents/test_foundry_agent.py` | **SYNC** |
| `backend/v4/magentic_agents/test_magentic_agent_factory.py` | **SYNC** |
| `backend/v4/magentic_agents/test_proxy_agent.py` | **SYNC** |
| `backend/v4/orchestration/__init__.py` | **SYNC** |
| `backend/v4/orchestration/helper/test_plan_to_mplan_converter.py` | **SYNC** |
| `backend/v4/orchestration/test_human_approval_manager.py` | **SYNC** |
| `backend/v4/orchestration/test_orchestration_manager.py` | **SYNC** |

### mcp_server/ (5 files)
| File | Decision |
|------|----------|
| `mcp_server/conftest.py` | **SYNC** |
| `mcp_server/test_factory.py` | **SYNC** |
| `mcp_server/test_fastmcp_run.py` | **SYNC** |
| `mcp_server/test_hr_service.py` | **SYNC** |
| `mcp_server/test_utils.py` | **SYNC** |

**Comando para restaurar:**
```bash
git checkout upstream/main -- src/tests/
```

---

## 5. SRC/FRONTEND - 83 files

### Deleted (39 files) - **SYNC**
| File | Decision | Rationale |
|------|----------|-----------|
| `.dockerignore` | **SYNC** | Upstream config |
| `public/contosoLogo.svg` | **SYNC** | Upstream asset |
| `src/components/common/PlanCancellationDialog.tsx` | **SYNC** | Upstream component |
| `src/components/common/TeamSelected.tsx` | **SYNC** | Upstream component |
| `src/components/common/TeamSelector.tsx` | **SYNC** | Upstream component |
| `src/components/content/PlanChatBody.tsx` | **SYNC** | Upstream component |
| `src/components/content/streaming/*.tsx` | **SYNC** | 6 streaming components |
| `src/components/errors/RAIErrorCard.tsx` | **SYNC** | Upstream component |
| `src/components/errors/index.tsx` | **SYNC** | Upstream component |
| `src/hooks/index.tsx` | **SYNC** | Upstream hooks |
| `src/hooks/usePlanCancellationAlert.tsx` | **SYNC** | Upstream hook |
| `src/hooks/useRAIErrorHandling.tsx` | **SYNC** | Upstream hook |
| `src/hooks/useTeamSelection.tsx` | **SYNC** | Upstream hook |
| `src/hooks/useWebSocket.tsx` | **SYNC** | Upstream hook |
| `src/models/Team.tsx` | **SYNC** | Upstream model |
| `src/services/TeamService.tsx` | **SYNC** | Upstream service |
| `src/services/WebSocketService.tsx` | **SYNC** | Upstream service |
| `src/styles/Panel.css` | **SYNC** | Upstream style |
| `src/styles/PlanCreatePage.css` | **SYNC** | Upstream style |
| `src/styles/RAIErrorCard.css` | **SYNC** | Upstream style |
| `src/styles/TeamSelector.module.css` | **SYNC** | Upstream style |
| `src/styles/planpanelright.css` | **SYNC** | Upstream style |
| `src/utils/agentIconUtils.tsx` | **SYNC** | Upstream utility |

### Added (1 file) - **KEEP**
| File | Decision | Rationale |
|------|----------|-----------|
| `src/components/content/TaskDetails.tsx` | **KEEP** | Custom component |
| `src/models/projectProfile.tsx` | **KEEP** | Custom model |

### Modified (42 files) - **REVIEW**
| File | Decision | Rationale |
|------|----------|-----------|
| `.env.sample` | REVIEW | Comparar variables |
| `.gitignore` | REVIEW | Comparar reglas |
| `Dockerfile` | REVIEW | Comparar build |
| `frontend_server.py` | REVIEW | Comparar server |
| `index.html` | REVIEW | Comparar HTML |
| `package-lock.json` | REVIEW | Comparar deps |
| `package.json` | REVIEW | Comparar deps |
| `public/index.html` | REVIEW | Comparar HTML |
| `pyproject.toml` | REVIEW | Comparar config |
| `src/App.tsx` | REVIEW | Comparar App |
| `src/api/*.tsx` | REVIEW | 3 API files |
| `src/components/**/*.tsx` | REVIEW | Multiple components |
| `src/coral/**/*.tsx` | REVIEW | Multiple coral components |
| `src/index.css` | REVIEW | Comparar styles |
| `src/index.tsx` | REVIEW | Comparar entry |
| `src/models/*.tsx` | REVIEW | Multiple models |
| `src/pages/*.tsx` | REVIEW | 3 pages |
| `src/services/*.tsx` | REVIEW | Services |
| `src/styles/*.css` | REVIEW | Multiple styles |
| `src/utils/errorUtils.tsx` | REVIEW | Comparar utils |
| `uv.lock` | REVIEW | Regenerar |
| `vite.config.ts` | REVIEW | Comparar config |

---

## 6. TESTS/E2E-TEST - 1 file

| File | Decision | Rationale |
|------|----------|-----------|
| `tests/test_MACAE_Smoke_test.py` | REVIEW | Comparar con upstream, mantener ajustes funcionales |

---

## 7. INFRA - 39 files

### Added (37 files) - **KEEP**
| File | Decision | Rationale |
|------|----------|-----------|
| `abbreviations.json` | **KEEP** | Naming conventions |
| `bicepconfig.json` | **KEEP** | Bicep configuration |
| `cleanup_wrong_resource_group.sh` | **KEEP** | Cleanup script |
| `deploy_function_apps.sh` | **KEEP** | Function deployment |
| `deploy_functions_code.sh` | **KEEP** | Functions code |
| `deploy_functions_code_improved.sh` | **KEEP** | Improved deployment |
| `deploy_strategic_autonomy.sh` | **KEEP** | Strategic autonomy |
| `deploy_with_runtime.sh` | **KEEP** | Runtime deployment |
| `deploy_zip.sh` | **KEEP** | Zip deployment |
| `modules/account/main.bicep` | **KEEP** | Account module |
| `modules/account/modules/dependencies.bicep` | **KEEP** | Dependencies |
| `modules/account/modules/keyVaultExport.bicep` | **KEEP** | KeyVault export |
| `modules/account/modules/project.bicep` | **KEEP** | Project module |
| `modules/ai-hub.bicep` | **KEEP** | AI Hub module |
| `modules/container-app-environment.bicep` | **KEEP** | Container App env |
| `modules/fetch-container-image.bicep` | **KEEP** | Image fetch |
| `modules/role.bicep` | **KEEP** | Role module |
| `old/*` | REVIEW | 10 files - Historical, verificar si necesarios |
| `scripts/DotEnv.psm1` | **KEEP** | PowerShell module |
| `scripts/README.md` | **KEEP** | Scripts documentation |
| `scripts/deploy_agents_endpoints_fixed.ps1` | **KEEP** | Agent deployment |
| `scripts/foundry_role_definition.json` | **KEEP** | Role definition |
| `scripts/generated_token.json` | REVIEW | Verificar si sensitive |
| `scripts/invoke-agent975.ps1` | **KEEP** | Agent invocation |
| `scripts/load_dotenv.ps1` | **KEEP** | Env loading |
| `scripts/main.ps1` | **KEEP** | Main script |

### Modified (2 files)
| File | Decision | Rationale |
|------|----------|-----------|
| `main.bicep` | REVIEW | Core infrastructure, comparar cuidadosamente |
| `scripts/Selecting-Team-Config-And-Data.ps1` | REVIEW | Comparar cambios |

---

## 8. SRC/AGENTS - 11 files (ALL ADDED)

**Status:** Directorio custom del fork (agent975 experimental).
**Decision:** **KEEP** - Custom fork experimentation.

| File | Decision |
|------|----------|
| `agent975/README.md` | **KEEP** |
| `agent975/RECURSON AZURE.txt` | **KEEP** |
| `agent975/analysis-result.json` | **KEEP** |
| `agent975/diagnose-agent975.mjs` | **KEEP** |
| `agent975/handler.js` | **KEEP** |
| `agent975/package-lock.json` | **KEEP** |
| `agent975/package.json` | **KEEP** |
| `agent975/run-agent975-azureml.mjs` | **KEEP** |
| `agent975/run-agent975-debug.mjs` | **KEEP** |
| `agent975/run-agent975-fixed.mjs` | **KEEP** |
| `agent975/run-agent975.mjs` | **KEEP** |

---

## 9. DOCS - 9 files (ALL ADDED)

**Status:** Documentación custom del fork.
**Decision:** **KEEP** - Fork documentation.

| File | Decision |
|------|----------|
| `CustomizeSolution.md` | **KEEP** |
| `CustomizeSolution_old.md` | REVIEW |
| `DEPLOYMENT_COMPLETE.md` | **KEEP** |
| `INFRASTRUCTURE_COMPLETE.md` | **KEEP** |
| `LocalDeployment.md` | **KEEP** |
| `STRATEGIC_AUTONOMY_ARCHITECTURE.md` | **KEEP** |
| `STRATEGIC_AUTONOMY_DESIGN_DECISIONS.md` | **KEEP** |
| `STRATEGIC_AUTONOMY_VALIDATION.md` | **KEEP** |
| `STRATEGIC_LOOP_VALIDATION_FINAL.md` | **KEEP** |

---

## 10. ROOT FILES - 26 files

### Added (14 files) - **KEEP**
| File | Decision | Rationale |
|------|----------|-----------|
| `.env.example` | **KEEP** | Environment template |
| `.yamllint.yml` | **KEEP** | YAML linting |
| `DEPLOY_BACKEND_GUIDE.md` | **KEEP** | Deployment guide |
| `FIBROSKIN_GAP_ANALYSIS.md` | **KEEP** | Analysis document |
| `QUICK_START_LOCAL.md` | **KEEP** | Quick start guide |
| `agents.context.json` | **KEEP** | Agent context |
| `fix_permissions.sh` | **KEEP** | Permission script |
| `ml_workspace_config.json` | **KEEP** | ML workspace config |
| `package.json` | **KEEP** | Root package.json |
| `pyrightconfig.json` | **KEEP** | Pyright config |
| `run-agent975.mjs` | **KEEP** | Agent runner |
| `simple-ai-foundry-integration.ps1` | **KEEP** | Integration script |
| `test-agent975.ps1` | **KEEP** | Agent test |
| `workspace_config.json` | **KEEP** | Workspace config |

### Deleted (4 files) - **SYNC**
| File | Decision | Rationale |
|------|----------|-----------|
| `.coveragerc` | **SYNC** | Coverage config |
| `azure_custom.yaml` | REVIEW | Verificar si necesario |
| `conftest.py` | **SYNC** | Pytest config |
| `awscliv2.zip` | REVIEW | Verificar si debe estar en repo |

### Modified (8 files) - **REVIEW**
| File | Decision | Rationale |
|------|----------|-----------|
| `.gitignore` | REVIEW | Comparar reglas |
| `Multi-Agent-Custom-Automation-Engine-Solution-Accelerator.code-workspace` | REVIEW | Comparar workspace |
| `TRANSPARENCY_FAQS.md` | REVIEW | Comparar contenido |
| `azure.yaml` | REVIEW | Core config, comparar cuidadosamente |
| `azure_env_variables.txt` | REVIEW | Verificar si sensitive |
| `azure_ml_report.txt` | REVIEW | Verificar si debe estar en repo |
| `package-lock.json` | REVIEW | Regenerar |
| `pytest.ini` | REVIEW | Comparar config |

---

## EXECUTION SUMMARY

### Phase 1: Immediate SYNC (Critical)
```bash
# Restaurar src/mcp_server (24 files)
git checkout upstream/main -- src/mcp_server/

# Restaurar src/tests (50 files)
git checkout upstream/main -- src/tests/
```

### Phase 2: Merge PR #33
- PR #33 actualiza SDK versions (semantic-kernel 1.39.4, openai 1.105.0)
- Checks están green
- Merge cuando esté listo

### Phase 3: Review Files (Pending)
- Total REVIEW items: ~100 files
- Prioridad: workflows, frontend, infra/main.bicep

### Arquitectura Backend Actual
- **Entry point:** `app_kernel.py` (KEEP)
- **Config:** `app_config.py` + `config_kernel.py` (KEEP)
- **Agents:** `kernel_agents/` (KEEP)
- **Tools:** `kernel_tools/` (KEEP)
- **Adapters:** `adapters/` (KEEP)
- **Connectors:** `connectors/` (KEEP)

Esta es la arquitectura del fork. La estructura upstream (v4/, common/) debe restaurarse via SYNC para compatibilidad, pero `app_kernel.py` permanece como entry point principal.
