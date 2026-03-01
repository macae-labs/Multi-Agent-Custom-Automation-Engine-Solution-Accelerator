# Ejemplo de integración en planner_agent.py
# Agregar después de crear los steps en _create_structured_plan()

"""
# En planner_agent.py, método _create_structured_plan, después de crear steps:

from kernel_agents.validator_agent import ValidatorAgent

# ... código existente que crea steps ...

# Validar asignaciones de agentes (fail-open)
try:
    validator = ValidatorAgent(self, self._available_agents)
    validation_result = await validator.validate_plan_batch(steps, self._agent_tools_list)
    
    # Aplicar correcciones y agregar auditoría
    corrections = ValidatorAgent.apply_corrections(steps, validation_result)
    
    if corrections > 0:
        logging.info(f"Validator applied {corrections} corrections to plan {plan.id}")
        
        # Actualizar steps en Cosmos con auditoría
        for step in steps:
            await self._memory_store.update_step(step)
    
    track_event_if_configured(
        "Planner - Validation completed",
        {
            "plan_id": plan.id,
            "total_steps": len(steps),
            "corrections_applied": corrections,
        },
    )
except Exception as e:
    # Fail-open: continuar con plan original si validator falla
    logging.warning(f"Validator failed for plan {plan.id}, proceeding with original: {e}")

return plan, steps
"""
