from typing import Any, Dict, List, Literal, Optional

from pydantic import Field
from semantic_kernel.kernel_pydantic import KernelBaseModel

from models.messages_kernel import BaseDataModel


class CredentialBinding(KernelBaseModel):
    """Binding of credentials to a project."""

    provider_id: str
    secret_uri: str
    is_active: bool = True


class ProjectProfile(BaseDataModel):
    """Project profile persisted in Cosmos for multi-tenant plugin injection."""

    data_type: Literal["project_profile"] = Field(default="project_profile")
    session_id: str
    user_id: str
    project_id: str
    project_name: str
    api_base_url: Optional[str] = None
    aws_s3_bucket: Optional[str] = None
    firestore_root: Optional[str] = None
    enabled_tools: List[str] = Field(default_factory=list)
    api_key: Optional[str] = None
    custom_config: Dict[str, Any] = Field(default_factory=dict)
    credential_bindings: List[CredentialBinding] = Field(default_factory=list)


class ProjectProfileUpsert(KernelBaseModel):
    """Payload used by frontend/backend API to upsert project context."""

    session_id: str
    project_id: str
    project_name: str
    api_base_url: Optional[str] = None
    aws_s3_bucket: Optional[str] = None
    firestore_root: Optional[str] = None
    enabled_tools: List[str] = Field(default_factory=list)
    api_key: Optional[str] = None
    custom_config: Dict[str, Any] = Field(default_factory=dict)
    credential_bindings: List[CredentialBinding] = Field(default_factory=list)
