import logging
from typing import Annotated
import httpx
from semantic_kernel.functions import kernel_function


class ExternalAPIPlugin:
    """Generic plugin to interact with external project APIs."""
    
    def __init__(self, base_url: str, api_key: str = None):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
    
    @kernel_function(
        name="call_external_api",
        description="Call an external project API endpoint"
    )
    async def call_api(
        self,
        endpoint: Annotated[str, "API endpoint path (e.g., /create-video)"],
        method: Annotated[str, "HTTP method (GET, POST, PUT, DELETE)"] = "POST",
        payload: Annotated[str, "JSON payload as string"] = "{}",
    ) -> str:
        """Call external API and return response."""
        import json
        
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    json=json.loads(payload) if payload else None
                )
                response.raise_for_status()
                return response.text
        except Exception as e:
            logging.error(f"External API call failed: {e}")
            return f"Error: {str(e)}"
