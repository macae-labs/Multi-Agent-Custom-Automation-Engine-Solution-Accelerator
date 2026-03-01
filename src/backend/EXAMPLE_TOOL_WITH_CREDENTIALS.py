"""Example tool with runtime credential resolution."""
import logging
from typing import Annotated
from semantic_kernel.functions import kernel_function
from credential_resolver import credential_resolver
from tool_registry import ToolRegistry


class SalesforceTools:
    """Salesforce tools with dynamic credential resolution."""
    
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.provider_id = "salesforce"
    
    async def _get_credentials(self) -> dict:
        """Resolve credentials from Key Vault."""
        credentials = await credential_resolver.resolve_credentials(
            self.project_id,
            self.provider_id
        )
        
        if not credentials:
            # Return structured error for UI to handle
            required_fields = ToolRegistry.get_required_credentials("salesforce_create_lead")
            raise CredentialsRequiredError(
                provider_id=self.provider_id,
                required_fields=[
                    {
                        "name": field.name,
                        "display_name": field.display_name,
                        "type": field.type.value,
                        "description": field.description,
                        "required": field.required,
                    }
                    for field in required_fields
                ],
                onboarding_url=f"/tools/connect/{self.provider_id}"
            )
        
        return credentials
    
    @kernel_function(
        name="create_salesforce_lead",
        description="Create a new lead in Salesforce CRM"
    )
    async def create_lead(
        self,
        first_name: Annotated[str, "First name of the lead"],
        last_name: Annotated[str, "Last name of the lead"],
        company: Annotated[str, "Company name"],
        email: Annotated[str, "Email address"],
    ) -> str:
        """Create a lead in Salesforce."""
        try:
            # Resolve credentials at runtime
            credentials = await self._get_credentials()
            
            # Use credentials to call Salesforce API
            instance_url = credentials.get("instance_url")
            access_token = credentials.get("access_token")
            
            # TODO: Actual Salesforce API call
            # response = requests.post(
            #     f"{instance_url}/services/data/v58.0/sobjects/Lead",
            #     headers={"Authorization": f"Bearer {access_token}"},
            #     json={"FirstName": first_name, "LastName": last_name, ...}
            # )
            
            logging.info(f"Created Salesforce lead: {first_name} {last_name}")
            return f"Successfully created lead for {first_name} {last_name} at {company}"
            
        except CredentialsRequiredError as e:
            # Return structured error that UI can parse
            return e.to_json()
        except Exception as e:
            logging.error(f"Failed to create Salesforce lead: {e}")
            return f"Error: {str(e)}"


class CredentialsRequiredError(Exception):
    """Exception raised when credentials are not configured."""
    
    def __init__(self, provider_id: str, required_fields: list, onboarding_url: str):
        self.provider_id = provider_id
        self.required_fields = required_fields
        self.onboarding_url = onboarding_url
        super().__init__(f"Credentials required for {provider_id}")
    
    def to_json(self) -> str:
        """Convert to JSON for structured error response."""
        import json
        return json.dumps({
            "error_type": "credentials_required",
            "provider_id": self.provider_id,
            "required_fields": self.required_fields,
            "onboarding_url": self.onboarding_url,
            "message": f"Please configure credentials for {self.provider_id}"
        })


# Usage in agent initialization:
"""
# In agent_factory.py or tool initialization:
salesforce_tools = SalesforceTools(project_id=session_id)
tools = [
    salesforce_tools.create_lead,
    # ... other tools
]
"""
