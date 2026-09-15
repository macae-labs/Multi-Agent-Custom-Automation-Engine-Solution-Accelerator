# Plano de control durable: objetivo persistente y reconciliación

Estado: diseño confirmado (2026-09-15). Incremento 1 implementado. Incrementos 2 a 4 pendientes.

## Por qué

Hoy la unidad de ejecución es el turno. Un request entra, el Model Router razona, llama tools, reintenta, responde, y nadie posee el objetivo después de la respuesta. Model Router, Magentic, agentes Foundry, MCP, Workspace, GitHub, Cosmos, telemetría y sondas son capacidades esperando una llamada. Lo que falta es el dueño del objetivo: un trabajo que sobrevive al turno, que decide qué evento lo despierta y que continúa hasta su condición terminal o hasta su techo de autoridad. El ciclo observar, detectar, diagnosticar, proponer, actuar, validar, comparar, aprender y REARMAR se hace hoy a mano.

## Hechos verificados (HEAD a47b4724)

- `run_orchestration` corre como `BackgroundTask` (`v4/api/router.py:869`): sobrevive a la respuesta HTTP, no al proceso.
- `wait_for_approval` es `asyncio.wait_for(event.wait(), timeout)` sobre `asyncio.Event` en memoria (`v4/config/settings.py:161`). La espera humana no es un estado: es un hilo bloqueado con reloj. Un reinicio o el timeout la matan. Es lo opuesto de REARM por identidad y causalidad.
- `init_orchestration` pasaba `InMemoryCheckpointStorage()` al `MagenticBuilder` (`orchestration_manager.py:321`, hoy `get_checkpoint_storage()`): los checkpoints morían con el proceso.
- Framework: `Workflow.run(message | responses, checkpoint_id, checkpoint_storage)`. `checkpoint_id` y `responses` en una sola llamada significan "restaurar y luego entregar respuestas". Un `WorkflowCheckpoint` guarda `workflow_name`, `graph_signature_hash`, `checkpoint_id`, `previous_checkpoint_id`, `timestamp`, `messages`, `state`, `pending_request_info_events`, `iteration_count`, `metadata`, `version`. Se crea al final de cada superstep.
- Precondición de reanudación, impuesta por el framework (`agent_framework/_workflows/_runner.py:275`): si `graph_signature_hash` del grafo reconstruido no coincide con el del checkpoint, `WorkflowCheckpointException("Workflow graph has changed since the checkpoint was created")`. Medido: el hash depende de los ids de los executors (los nombres de los participantes) y de las aristas; no depende del nombre del workflow. Dos builds del mismo grafo dan el mismo hash; renombrar un participante lo cambia.
- Nombre del workflow: `MagenticBuilder` no lo fija, así que cada build recibe `WorkflowBuilder-<uuid>` nuevo. La partición `/workflow_name` del contenedor de checkpoints es por build, no por corrida lógica. La identidad de la corrida la lleva el work item: `checkpoint_id` y la cadena `previous_checkpoint_id`.
- HITL nativo del framework: `MagenticBuilder(enable_plan_review=True)` hace que el orquestador llame `ctx.request_info(MagenticPlanReviewRequest(plan, current_progress, is_stalled), MagenticPlanReviewResponse)` (`_magentic.py:1030`). El workflow queda inactivo con la petición pendiente, el checkpoint conserva `pending_request_info_events`, y se continúa con `run(checkpoint_id=…, responses={request_id: MagenticPlanReviewResponse.approve() | .revise(feedback)})`. El producto no usa esto: `HumanApprovalMagenticManager.plan()` bloquea dentro del executor esperando `wait_for_approval`, por lo que ningún superstep termina y no existe checkpoint durante la espera.
- `incident.v1` ya es consumible por máquina: `learn.executable_probe` trae comando, cwd y señales esperadas; `expires_if_not_reverified_by` trae la condición de revalidación; `authority_ceiling` trae el techo. Nada lo consume todavía.

## Modelo

`work_item` (Cosmos, `data_type` nuevo, partición por `user_id` como el resto):

| Campo | Contenido |
|---|---|
| `objective` | Texto del objetivo y `desired_state` verificable |
| `invariants` | Referencias a INC (`INC-2026-00N`) cuyas sondas definen "sano" |
| `evidence` | Referencias a sondas ejecutadas, logs, checkpoints, PRs |
| `pending_actions` | Acciones con clase (`read-only`, `write-scratch`, `write-shared`) |
| `authority_ceiling` | Igual que en `incident.v1` |
| `team_id`, `team_config_hash` | Identidad del equipo con el que se construyó el grafo |
| `checkpoint_id` | Último checkpoint durable de la corrida Magentic |
| `waiting_for` | El REARM: `{kind, identity, fallback_timer}` |
| `status` | `running`, `waiting_for_event`, `waiting_for_human`, `blocked`, `done` |

`work_event` (append-only): `work_item_id`, `kind`, `identity`, `payload`, `evidence`, `timestamp`. Kinds: `github_push`, `ci_run`, `revision_change`, `approval`, `incident_expiry`, `tool_failure`, `timer_fallback`, `reconciled`. El estado del work item es el pliegue de su historial de eventos, que es el principio de Temporal sin traer Temporal.

`waiting_for` se define por identidad y causalidad: un SHA, un `run_id` de CI, una revisión, un `plan_id`, un `request_id`, un `incident_id`. El reloj existe sólo como respaldo de un evento que no llegó.

## Ciclo

| Primitiva | Pieza existente que la realiza |
|---|---|
| `observe()` | Ejecuta `learn.executable_probe` del INC pertinente; consulta `exceptions` y `dependencies` en Log Analytics |
| `retrieve_relevant_history()` | Búsqueda por `retrieval_keys` y `structural_match` en `docs/incidents/*.json` más el historial del work item |
| `choose_action()` | Model Router o Magentic con el work item como tarea; el plano de ejecución no cambia |
| `check_authority()` | Compara la clase de la acción con `max_action_class_without_human`; si la excede, persiste `waiting_for_human` |
| `execute()` | Tools existentes: MCP, `workspace_exec`, GitHub, sondas |
| `verify()` | Escalera L0 a L7 con control negativo, como exige el schema |
| `persist()` | Apila `work_event` y guarda el checkpoint |
| `rearm()` | Escribe `waiting_for` |

Los INC enriquecen lo que el ciclo recupera; no modifican el motor. Un incidente nuevo es una entrada más en el registro, no una regla más en el código.

## Entrada de eventos y loop

Un único endpoint `POST /api/v4/events` recibe todo: webhooks de GitHub, cambios de revisión, alertas de App Insights, aprobaciones humanas, vencimientos de INC, fallos de tool. Valida, apila el `work_event` y despierta al reconciliador. La durabilidad está en el evento persistido, no en la señal en memoria.

El reconciliador vive en el `lifespan` del backend con un lease en Cosmos (documento con etag; el backend corre con una réplica). Cada iteración carga los work items no terminales cuyo `waiting_for` coincide con el evento recibido, ejecuta el ciclo, persiste y rearma. Termina cuando alcanza un estado estable o su techo de autoridad.

## Precondición de reanudación, explícita

Antes de `run(checkpoint_id=…)` el reconciliador compara `team_id` y `team_config_hash` del work item con la team-config actual. El hash es canónico: nombres y orden de participantes más `deployment_name`. Si no coinciden, no reanuda: replanifica desde el último estado conocido, con un workflow nuevo y el contexto sembrado desde `messages` del checkpoint. El framework valida además `graph_signature_hash`; el work item verifica antes, por identidad de equipo, para no depender de la excepción. Sin esto el incremento 1 funciona hasta el primer cambio de equipo.

## Aprobación como estado, no como espera

La aprobación es un `work_event` de kind `approval` con `request_id` y `plan_id`. Con `enable_plan_review=True` el orquestador emite `request_info`; `run_orchestration`, al ver ese evento, persiste `waiting_for = {kind: approval, request_id, checkpoint_id}` y termina, liberando el `BackgroundTask`. La aprobación entra por `/api/v4/events` y el reconciliador reanuda con `run(checkpoint_id, responses={request_id: approve() | revise(feedback)})`. `wait_for_approval`, `set_approval_pending` y `_approval_events` dejan de existir. El endpoint actual de aprobación del plan pasa a producir el `work_event`; el contrato hacia la UI no cambia. Respaldar `approvals` con un documento manteniendo el `asyncio.Event` seguiría siendo un hilo esperando, sólo que con un documento al lado.

## Incrementos y verificación

1. `CosmosCheckpointStorage`. Hecho: `common/services/checkpoint_storage.py`, seis métodos del protocolo, contenedor propio `workflow_checkpoints`, partición `/workflow_name`, misma codificación que `FileCheckpointStorage`. Sustituye la línea 321 sin cambio de contrato hacia afuera; el cliente se cierra en el `lifespan`. La credencial es prestada: `config.get_cosmos_credential_async()` devuelve `COSMOSDB_KEY` en dev y, si no, la credencial compartida del proceso, que sólo `aclose_shared_resources` cierra. Ese resolutor único reemplazó las tres copias del mismo `if` que fabricaban una credencial propia por servicio (`chat_cosmos_service`, `mcp_connections_service`, el storage), el patrón de `shared-credential-kill`. Verificado con `src/tests/backend/common/services/test_checkpoint_storage.py` contra el framework real: los checkpoints persisten en el contenedor, una instancia nueva del mismo grafo reanuda por `checkpoint_id`, y un grafo distinto es rechazado por el framework. Pendiente y de clase write-shared: la primera ejecución contra Cosmos de producción crea el contenedor, y el E2E de matar el proceso a mitad de un plan y reanudar por `checkpoint_id` escribe en Cosmos prod. Ambos con aprobación previa.
2. Aprobación como evento. Medido antes de tocar el producto con `src/tests/backend/v4/orchestration/test_plan_review_durable_harness.py`: un `ScriptedChatClient` del framework (sin Foundry) alimenta un `StandardMagenticManager` real con `enable_plan_review=True` sobre `CosmosCheckpointStorage`. El orquestador emite `request_info` con `MagenticPlanReviewRequest`, el superstep termina y el checkpoint conserva la petición pendiente con su `request_id`; una instancia nueva del mismo grafo hace `run(checkpoint_id, responses={request_id: approve()})` y llega a la respuesta final escribiendo más checkpoints; `revise(feedback)` replanifica y emite una petición nueva con otro `request_id`; un participante renombrado es rechazado. Lo que sobrevive de `HumanApprovalMagenticManager`: `_apply_pending_history` y `seed_chat_history` (siembra del contexto), `create_progress_ledger` (tope de rondas y agentes no consultados), `plan_to_obj` (la conversión a `MPlan` que la UI consume) y `prepare_final_answer`. Lo que se mueve al manejador del evento `request_info` en `run_orchestration`: la conversión del ledger a `MPlan`, el registro del plan, el `PLAN_APPROVAL_REQUEST` por WebSocket y la persistencia de `waiting_for`. Lo que desaparece: `plan()` bloqueante, `_wait_for_user_approval`, `wait_for_approval`, `set_approval_pending`, `_approval_events`, el `TIMEOUT_NOTIFICATION` por reloj. Pendiente de la misma familia: la clarificación al usuario (`_clarification_events`, `user_clarification`) también es una espera en proceso. Verificación del incremento: plan con aprobación pendiente, reinicio del backend, aprobación, y la corrida continúa desde el checkpoint; la UI recibe los mismos tipos de mensaje que hoy.
3. `work_item`, `work_event`, `/api/v4/events` y el loop con lease. Verificación: un evento con identidad produce una y sólo una reconciliación; un reinicio a mitad de iteración retoma desde el último evento persistido.
4. Primer controlador: revalidación de INC. `expires_if_not_reverified_by` vencido produce `incident_expiry`; el reconciliador ejecuta `executable_probe.command_or_test`; con señal sana avanza `last_verified`; con señal de fallo pone `status: needs_revalidation` y `learn.operational: false`, que es un invariante del schema, y deja el work item en `waiting_for_human` si el techo lo exige. Es el controlador más barato porque el artefacto ya es ejecutable, y es el que convierte `incident.v1` en estado operacional consumido por máquina. Verificación: un INC vencido en un fixture produce la transición; control negativo con la sonda de INC-2026-004 y `src/tests/backend/auth/__init__.py` restaurado.

## Qué no cambia

Model Router, Magentic, agentes Foundry y MCP siguen siendo el plano de ejecución. Temporal entra sólo si el reconciliador de un proceso deja de bastar por réplicas o volumen.
