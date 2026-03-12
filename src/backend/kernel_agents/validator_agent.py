"""Validator Agent for step assignment validation."""

import json
import logging
from typing import Any, List, Optional

from semantic_kernel.kernel_pydantic import KernelBaseModel

from models.messages_kernel import AgentType, Step
from utils.pii_redactor import PIIRedactor, get_pii_context


class StepValidation(KernelBaseModel):
    """Validation result for a single step."""

    step_index: int
    is_valid: bool
    confidence: float  # 0.0 to 1.0
    recommended_agent: Optional[AgentType] = None
    rationale: str


class PlanValidationResult(KernelBaseModel):
    """Batch validation result for entire plan."""

    validations: List[StepValidation]


class ValidatorAgent:
    """Validates Planner's agent assignments using LLM reasoning (batch mode)."""

    CORRECTION_THRESHOLD = 0.85  # Only correct if confidence >= 85%

    def __init__(self, planner_agent, available_agents: List[str]):
        self._planner = planner_agent
        self._available_agents = available_agents

    async def validate_plan_batch(
        self,
        steps: List[Step],
        agent_tools: dict[Any, Any],
        session_id: Optional[str] = None,
    ) -> PlanValidationResult:
        """Validate all step assignments in a single LLM call.

        Args:
            steps: List of steps to validate
            agent_tools: Dict of {AgentType: json_doc_string}

        Returns:
            PlanValidationResult with validation for each step (fail-open on error)
        """
        if not steps:
            return PlanValidationResult(validations=[])

        try:
            # Normalize tools from JSON strings to readable format
            normalized_tools = self._normalize_tools(agent_tools)

            # Build batch validation prompt
            pii_redactor = PIIRedactor()
            pii_context = get_pii_context(session_id) if session_id else None
            steps_summary_lines = []
            for i, step in enumerate(steps):
                action_for_validator = (
                    pii_context.redact(step.action)
                    if pii_context is not None
                    else pii_redactor.redact(step.action).redacted_text
                )
                steps_summary_lines.append(
                    f"{i}. Action: {action_for_validator} | Assigned: {step.agent.value}"
                )
            steps_summary = "\n".join(steps_summary_lines)

            validation_prompt = (
                f"Evaluate agent assignments for {len(steps)} steps in this plan.\n\n"
                f"Steps:\n{steps_summary}\n\n"
                f"Available agents and capabilities:\n{normalized_tools}\n\n"
                f"Allowed agents: {', '.join(self._available_agents)}\n\n"
                "For each step, provide:\n"
                "- step_index: the step number (0-based)\n"
                "- is_valid: true if assignment is correct, false otherwise\n"
                "- confidence: 0.0-1.0 score of your assessment\n"
                "- recommended_agent: ONLY if is_valid=false, suggest better agent from allowed list\n"
                "- rationale: brief explanation (max 20 words)\n\n"
                "Rules:\n"
                "- Only recommend agents from the allowed list\n"
                "- Consider tool availability and action requirements\n"
                "- Be conservative: mark valid unless clearly wrong"
            )

            response_content = ""
            polling_options = None
            if hasattr(self._planner, "_get_polling_options"):
                polling_options = self._planner._get_polling_options()
            async_generator = self._planner.invoke(
                messages=validation_prompt,
                thread=None,
                polling_options=polling_options,
                response_format=self._get_validation_schema(),
            )
            async for chunk in async_generator:
                if chunk:
                    response_content += str(chunk)

            result = PlanValidationResult.model_validate_json(response_content)

            # Log corrections with high confidence
            for validation in result.validations:
                if (
                    0 <= validation.step_index < len(steps)
                    and not validation.is_valid
                    and validation.confidence >= self.CORRECTION_THRESHOLD
                ):
                    step = steps[validation.step_index]
                    logging.info(
                        f"Validator suggests correction for step {validation.step_index}: "
                        f"{step.agent.value} → {validation.recommended_agent} "
                        f"(confidence: {validation.confidence:.2f})"
                    )

            return result

        except Exception as e:
            # Fail-open: return all valid if validator fails
            logging.warning(f"Validator failed, proceeding with original plan: {e}")
            return PlanValidationResult(
                validations=[
                    StepValidation(
                        step_index=i,
                        is_valid=True,
                        confidence=0.5,
                        rationale="Validator unavailable, using Planner assignment",
                    )
                    for i in range(len(steps))
                ]
            )

    @staticmethod
    def _agent_label(agent_type: Any) -> str:
        if isinstance(agent_type, AgentType):
            return agent_type.value
        return str(agent_type)

    def _normalize_tools(self, agent_tools: dict[Any, Any]) -> str:
        """Parse JSON tool docs into readable capability list."""
        lines = []
        for agent_type, json_doc in agent_tools.items():
            agent_label = self._agent_label(agent_type)
            try:
                # Parse JSON string to extract function names
                if isinstance(json_doc, str):
                    tools_data = json.loads(json_doc)
                    if isinstance(tools_data, list):
                        # Support both "name" (Azure format) and "function" (local format)
                        all_tool_names = [
                            t.get("name") or t.get("function", "unknown")
                            for t in tools_data
                            if isinstance(t, dict)
                        ]
                        # Show all tools (max 30 to avoid prompt bloat)
                        tool_names = all_tool_names[:30]
                        if len(all_tool_names) > 30:
                            tool_names.append(f"...+{len(all_tool_names) - 30} more")
                    else:
                        tool_names = ["(see full doc)"]
                else:
                    tool_names = ["(non-JSON format)"]

                lines.append(f"- {agent_label}: {', '.join(tool_names)}")
            except Exception as e:
                logging.warning(f"Failed to parse tools for {agent_label}: {e}")
                lines.append(f"- {agent_label}: (tools available)")

        return "\n".join(lines)

    def _get_validation_schema(self):
        """Get JSON schema for batch validation response."""

        from azure.ai.agents.models import (
            ResponseFormatJsonSchema,
            ResponseFormatJsonSchemaType,
        )

        return ResponseFormatJsonSchemaType(
            json_schema=ResponseFormatJsonSchema(
                name=PlanValidationResult.__name__,
                description="Batch validation result for plan steps",
                schema=PlanValidationResult.model_json_schema(),
            )
        )

    @staticmethod
    def apply_corrections(
        steps: List[Step], validation_result: PlanValidationResult
    ) -> int:
        """Apply high-confidence corrections to steps and add audit trail.

        Args:
            steps: List of steps to potentially correct
            validation_result: Validation results from batch validation

        Returns:
            Number of corrections applied
        """
        corrections = 0

        for validation in validation_result.validations:
            if validation.step_index < 0 or validation.step_index >= len(steps):
                continue

            step = steps[validation.step_index]

            # Add audit trail
            step.planner_rationale = f"Planner assigned {step.agent.value}"
            step.validator_decision = validation.rationale
            step.confidence_score = validation.confidence

            # Apply correction only if high confidence and invalid
            if (
                not validation.is_valid
                and validation.confidence >= ValidatorAgent.CORRECTION_THRESHOLD
                and validation.recommended_agent
            ):
                original_agent = step.agent
                step.agent = validation.recommended_agent
                corrections += 1

                logging.info(
                    f"Applied correction to step {step.id}: "
                    f"{original_agent.value} → {validation.recommended_agent.value}"
                )

        return corrections
