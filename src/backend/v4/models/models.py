import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PlanStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MStep(BaseModel):
    """model of a step in a plan"""

    agent: str = ""
    action: str = ""


class MPlan(BaseModel):
    """model of a plan"""

    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    team_id: str = ""
    plan_id: str = ""
    overall_status: PlanStatus = PlanStatus.CREATED
    user_request: str = ""
    team: list[str] = []
    facts: str = ""
    steps: list[MStep] = []
