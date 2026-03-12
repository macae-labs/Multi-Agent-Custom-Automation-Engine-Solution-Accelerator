"""
Cloud Functions Plugin - Calls existing Firebase Cloud Functions.

This plugin allows agents to invoke existing Cloud Functions for business logic
operations (writes, payments, emails, etc.) while using the Firestore plugin
for read/analysis operations.

Architecture:
- Cloud Functions: createOrder, sendEmails, initStripePayment, etc.
- Firestore Plugin: query_firestore_docs, read_firestore_doc, etc.
"""

import logging
import os
from dataclasses import dataclass
from typing import Annotated, Optional, Dict
import httpx
from semantic_kernel.functions import kernel_function

# Try to import Google Auth for Cloud Run authentication
try:
    import google.auth as _google_auth  # noqa: F401 - availability check
    GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    GOOGLE_AUTH_AVAILABLE = False


@dataclass
class FunctionConfig:
    """Configuration for a single Cloud Function."""
    url: str
    method: str = "POST"
    require_auth: bool = False


class CloudFunctionsPlugin:
    """Plugin to invoke Firebase Cloud Functions from agents.

    Uses explicit URL mapping per function instead of assuming naming conventions.
    Each function has its own auth requirement.
    """

    # Explicit function registry: tool_name -> FunctionConfig
    # This is the SINGLE SOURCE OF TRUTH for all Cloud Function URLs
    FUNCTION_REGISTRY: Dict[str, FunctionConfig] = {
        "getMentors": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/getMentors",
            method="POST",
            require_auth=False,
        ),
        "getCourses": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/getCourses",
            method="POST",
            require_auth=False,
        ),
        "getCourseDetail": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/getCourseDetail",
            method="POST",
            require_auth=False,
        ),
        "getProducts": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/getProducts",
            method="POST",
            require_auth=False,
        ),
        "getProductDetail": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/getProductDetail",
            method="POST",
            require_auth=False,
        ),
        "getOrders": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/getOrders",
            method="POST",
            require_auth=False,
        ),
        "getCategories": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/getCategories",
            method="POST",
            require_auth=False,
        ),
        "getContactInfo": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/getContactInfo",
            method="POST",
            require_auth=False,
        ),
        "getUpcomingEvents": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/getUpcomingEvents",
            method="POST",
            require_auth=False,
        ),
        "checkAvailability": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/checkAvailability",
            method="POST",
            require_auth=False,
        ),
        "createOrder": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/createOrder",
            method="POST",
            require_auth=False,
        ),
        "getOrCreateCustomer": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/getOrCreateCustomer",
            method="POST",
            require_auth=False,
        ),
        "chat": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/chat",
            method="POST",
            require_auth=False,
        ),
        "sendEmails": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/sendEmails",
            method="POST",
            require_auth=False,
        ),
        "changeEmail": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/changeEmail",
            method="POST",
            require_auth=False,
        ),
        "forgotPassword": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/forgotPassword",
            method="POST",
            require_auth=False,
        ),
        "findUserById": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/findUserById",
            method="POST",
            require_auth=False,
        ),
        "initStripePayment": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/initStripePayment",
            method="POST",
            require_auth=False,
        ),
        "initStripeTestPayment": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/initStripeTestPayment",
            method="POST",
            require_auth=False,
        ),
        "createStripeCustomer": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/createStripeCustomer",
            method="POST",
            require_auth=False,
        ),
        # Additional v2 functions (Cloud Run)
        "getAgentCRMProducts": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/getAgentCRMProducts",
            method="POST",
            require_auth=False,
        ),
        "getTutorials": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/getTutorials",
            method="POST",
            require_auth=False,
        ),
        # v1 function for push notifications
        "addFcmToken": FunctionConfig(
            url="https://us-central1-eng-gate-453810-h3.cloudfunctions.net/addFcmToken",
            method="POST",
            require_auth=False,
        ),
    }

    def __init__(
        self,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.project_id = project_id or os.getenv("FIREBASE_PROJECT_ID", "eng-gate-453810-h3")
        self.session_id = session_id
        self.user_id = user_id
        self.timeout = timeout
        self._logger = logging.getLogger(__name__)

    def _get_auth_token(self, audience: str) -> Optional[str]:
        """Get Google Cloud identity token for authenticated requests."""
        if not GOOGLE_AUTH_AVAILABLE:
            return None

        try:
            import google.auth
            from google.auth.transport.requests import Request as GoogleAuthRequest
            from google.oauth2 import id_token

            credentials, _ = google.auth.default()
            auth_req = GoogleAuthRequest()
            credentials.refresh(auth_req)
            token = id_token.fetch_id_token(auth_req, audience)
            return token
        except Exception as e:
            self._logger.error(f"Failed to get auth token for {audience}: {e}")
            return None

    async def _call_function(
        self,
        function_name: str,
        payload: dict,
    ) -> dict:
        """Internal method to call a Cloud Function using explicit registry."""

        # Get function config from registry
        config = self.FUNCTION_REGISTRY.get(function_name)
        if not config:
            return {
                "success": False,
                "error": f"Unknown function '{function_name}'. Not in FUNCTION_REGISTRY."
            }

        url = config.url
        config.method = config.method
        headers: Dict[str, str] = {"Content-Type": "application/json"}

        # Handle authentication - FAIL IMMEDIATELY if required but unavailable
        if config.require_auth:
            token = self._get_auth_token(url)
            if not token:
                return {
                    "success": False,
                    "error": f"Function '{function_name}' requires authentication but failed to obtain token. "
                             f"Ensure GOOGLE_APPLICATION_CREDENTIALS is set or running in GCP environment."
                }
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # Firebase Callable Functions always use POST with {"data": payload}
                response = await client.post(
                    url,
                    json={"data": payload},
                    headers=headers
                )
                response.raise_for_status()

                if response.status_code == 204 or not response.text:
                    return {"success": True, "data": {}}

                body = response.json()
                # Firebase callable returns {"result": ...}
                data = body.get("result", body)
                return {"success": True, "data": data}

        except httpx.HTTPStatusError as e:
            error_text = e.response.text[:300] if e.response.text else "No response body"
            self._logger.error(f"Cloud Function {function_name} returned {e.response.status_code}: {error_text}")
            return {"success": False, "error": f"HTTP {e.response.status_code}: {error_text}"}
        except httpx.TimeoutException:
            self._logger.error(f"Cloud Function {function_name} timed out after {self.timeout}s")
            return {"success": False, "error": f"Timeout after {self.timeout}s"}
        except Exception as e:
            self._logger.error(f"Cloud Function {function_name} failed: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # BUSINESS OPERATIONS (Write/Complex Logic)
    # =========================================================================

    @kernel_function(
        name="create_order",
        description="Create a new order for a customer. Use this for purchases and enrollments."
    )
    async def create_order(
        self,
        customer_id: Annotated[str, "Customer ID"],
        items: Annotated[str, "JSON array of items [{product_id, quantity, price}]"],
        payment_method: Annotated[str, "Payment method (stripe, cash, voucher)"] = "stripe",
    ) -> str:
        """Create order via Cloud Function - handles payment integration."""
        import json
        try:
            items_list = json.loads(items) if isinstance(items, str) else items
        except Exception:
            return "Error: items must be a valid JSON array"

        result = await self._call_function(
            "createOrder",
            {"customerId": customer_id, "items": items_list, "paymentMethod": payment_method}
        )

        if result["success"]:
            return f"Order created successfully: {result['data']}"
        return f"Failed to create order: {result['error']}"

    @kernel_function(
        name="send_emails",
        description="Send emails to users. Use for notifications, campaigns, or transactional emails."
    )
    async def send_emails(
        self,
        to: Annotated[str, "Recipient email or comma-separated list"],
        subject: Annotated[str, "Email subject"],
        body: Annotated[str, "Email body (HTML supported)"],
        template: Annotated[str, "Optional template name"] = "",
    ) -> str:
        """Send emails via Cloud Function."""
        payload = {
            "to": to.split(",") if "," in to else [to],
            "subject": subject,
            "body": body,
        }
        if template:
            payload["template"] = template

        result = await self._call_function("sendEmails", payload)

        if result["success"]:
            return f"Emails sent successfully to {to}"
        return f"Failed to send emails: {result['error']}"

    @kernel_function(
        name="init_stripe_payment",
        description="Initialize a Stripe payment session for a customer."
    )
    async def init_stripe_payment(
        self,
        customer_id: Annotated[str, "Customer ID"],
        amount: Annotated[int, "Amount in cents"],
        currency: Annotated[str, "Currency code (usd, mxn)"] = "usd",
        description: Annotated[str, "Payment description"] = "",
    ) -> str:
        """Initialize Stripe payment via Cloud Function."""
        result = await self._call_function(
            "initStripePayment",
            {
                "customerId": customer_id,
                "amount": amount,
                "currency": currency,
                "description": description,
            }
        )

        if result["success"]:
            return f"Payment session created: {result['data']}"
        return f"Failed to init payment: {result['error']}"

    @kernel_function(
        name="forgot_password",
        description="Send password reset email to a user."
    )
    async def forgot_password(
        self,
        email: Annotated[str, "User's email address"],
    ) -> str:
        """Trigger password reset via Cloud Function."""
        result = await self._call_function("forgotPassword", {"email": email})

        if result["success"]:
            return f"Password reset email sent to {email}"
        return f"Failed to send reset email: {result['error']}"

    @kernel_function(
        name="change_email",
        description="Change a user's email address."
    )
    async def change_email(
        self,
        user_id: Annotated[str, "User ID"],
        new_email: Annotated[str, "New email address"],
    ) -> str:
        """Change user email via Cloud Function."""
        result = await self._call_function(
            "changeEmail",
            {"userId": user_id, "newEmail": new_email}
        )

        if result["success"]:
            return f"Email changed successfully for user {user_id}"
        return f"Failed to change email: {result['error']}"

    # =========================================================================
    # READ OPERATIONS (via Cloud Functions when logic is needed)
    # =========================================================================

    @kernel_function(
        name="get_courses",
        description="Get list of available courses."
    )
    async def get_courses(
        self,
        category: Annotated[str, "Filter by category (optional)"] = "",
        limit: Annotated[int, "Maximum results"] = 20,
    ) -> str:
        """Get courses via Cloud Function."""
        payload: dict[str, str | int] = {"limit": limit}
        if category:
            payload["category"] = category

        result = await self._call_function("getCourses", payload)

        if result["success"]:
            return str(result["data"])
        return f"Failed to get courses: {result['error']}"

    @kernel_function(
        name="get_course_detail",
        description="Get detailed information about a specific course."
    )
    async def get_course_detail(
        self,
        course_id: Annotated[str, "Course ID"],
    ) -> str:
        """Get course details via Cloud Function."""
        result = await self._call_function(
            "getCourseDetail",
            {"courseId": course_id}
        )

        if result["success"]:
            return str(result["data"])
        return f"Failed to get course detail: {result['error']}"

    @kernel_function(
        name="get_products",
        description="Get list of available products."
    )
    async def get_products(
        self,
        category: Annotated[str, "Filter by category (optional)"] = "",
        limit: Annotated[int, "Maximum results"] = 20,
    ) -> str:
        """Get products via Cloud Function."""
        payload: dict[str, str | int] = {"limit": limit}
        if category:
            payload["category"] = category

        result = await self._call_function("getProducts", payload)

        if result["success"]:
            return str(result["data"])
        return f"Failed to get products: {result['error']}"

    @kernel_function(
        name="get_orders",
        description="Get orders for a customer."
    )
    async def get_orders(
        self,
        customer_id: Annotated[str, "Customer ID"],
        status: Annotated[str, "Filter by status (pending, completed, cancelled)"] = "",
    ) -> str:
        """Get customer orders via Cloud Function."""
        payload = {"customerId": customer_id}
        if status:
            payload["status"] = status

        result = await self._call_function("getOrders", payload)

        if result["success"]:
            return str(result["data"])
        return f"Failed to get orders: {result['error']}"

    @kernel_function(
        name="get_mentors",
        description="Get list of available mentors."
    )
    async def get_mentors(self) -> str:
        """Get mentors via Cloud Function."""
        result = await self._call_function("getMentors", {})
        if result["success"]:
            return str(result["data"])
        return f"Failed to get mentors: {result['error']}"

    @kernel_function(
        name="get_categories",
        description="Get list of available categories."
    )
    async def get_categories(self) -> str:
        """Get categories via Cloud Function."""
        result = await self._call_function("getCategories", {})
        if result["success"]:
            return str(result["data"])
        return f"Failed to get categories: {result['error']}"

    @kernel_function(
        name="check_availability",
        description="Check availability for a course or event."
    )
    async def check_availability(
        self,
        item_id: Annotated[str, "Course or event ID"],
        date: Annotated[str, "Date to check (YYYY-MM-DD)"] = "",
    ) -> str:
        """Check availability via Cloud Function."""
        payload = {"itemId": item_id}
        if date:
            payload["date"] = date

        result = await self._call_function("checkAvailability", payload)

        if result["success"]:
            return str(result["data"])
        return f"Failed to check availability: {result['error']}"

    @kernel_function(
        name="find_user_by_id",
        description="Find a user by their ID."
    )
    async def find_user_by_id(
        self,
        user_id: Annotated[str, "User ID to look up"],
    ) -> str:
        """Find user via Cloud Function."""
        result = await self._call_function("findUserById", {"userId": user_id})

        if result["success"]:
            return str(result["data"])
        return f"Failed to find user: {result['error']}"

    @kernel_function(
        name="get_or_create_customer",
        description="Get existing customer or create new one."
    )
    async def get_or_create_customer(
        self,
        email: Annotated[str, "Customer email"],
        name: Annotated[str, "Customer name"] = "",
    ) -> str:
        """Get or create customer via Cloud Function."""
        payload = {"email": email}
        if name:
            payload["name"] = name

        result = await self._call_function("getOrCreateCustomer", payload)

        if result["success"]:
            return str(result["data"])
        return f"Failed to get/create customer: {result['error']}"

    # =========================================================================
    # REMAINING WRAPPERS (6 missing functions)
    # =========================================================================

    @kernel_function(
        name="get_product_detail",
        description="Get detailed information about a specific product including price, description, and availability."
    )
    async def get_product_detail(
        self,
        product_id: Annotated[str, "Product ID or slug"],
    ) -> str:
        """Get product details via Cloud Function."""
        result = await self._call_function(
            "getProductDetail",
            {"productId": product_id}
        )

        if result["success"]:
            return str(result["data"])
        return f"Failed to get product detail: {result['error']}"

    @kernel_function(
        name="get_contact_info",
        description="Get business contact information including address, phone, email, and social media links."
    )
    async def get_contact_info(self) -> str:
        """Get contact info via Cloud Function."""
        result = await self._call_function("getContactInfo", {})

        if result["success"]:
            return str(result["data"])
        return f"Failed to get contact info: {result['error']}"

    @kernel_function(
        name="get_upcoming_events",
        description="Get list of upcoming events, workshops, and training sessions."
    )
    async def get_upcoming_events(
        self,
        limit: Annotated[int, "Maximum number of events to return"] = 10,
    ) -> str:
        """Get upcoming events via Cloud Function."""
        result = await self._call_function("getUpcomingEvents", {"limit": limit})

        if result["success"]:
            return str(result["data"])
        return f"Failed to get upcoming events: {result['error']}"

    @kernel_function(
        name="chat_with_assistant",
        description="Send a message to the business chat assistant for customer support queries."
    )
    async def chat_with_assistant(
        self,
        message: Annotated[str, "User message or question"],
        session_id: Annotated[str, "Chat session ID for context continuity"] = "",
    ) -> str:
        """Chat with assistant via Cloud Function."""
        payload = {"message": message}
        if session_id:
            payload["sessionId"] = session_id

        result = await self._call_function("chat", payload)

        if result["success"]:
            return str(result["data"])
        return f"Chat failed: {result['error']}"

    @kernel_function(
        name="init_stripe_test_payment",
        description="Initialize a Stripe TEST payment session for development/testing purposes."
    )
    async def init_stripe_test_payment(
        self,
        customer_id: Annotated[str, "Customer ID"],
        amount: Annotated[int, "Amount in cents"],
        currency: Annotated[str, "Currency code (usd, mxn)"] = "usd",
        description: Annotated[str, "Payment description"] = "Test payment",
    ) -> str:
        """Initialize Stripe TEST payment via Cloud Function."""
        result = await self._call_function(
            "initStripeTestPayment",
            {
                "customerId": customer_id,
                "amount": amount,
                "currency": currency,
                "description": description,
            }
        )

        if result["success"]:
            return f"Test payment session created: {result['data']}"
        return f"Failed to init test payment: {result['error']}"

    @kernel_function(
        name="create_stripe_customer",
        description="Create a new Stripe customer record for payment processing."
    )
    async def create_stripe_customer(
        self,
        email: Annotated[str, "Customer email address"],
        name: Annotated[str, "Customer full name"] = "",
        phone: Annotated[str, "Customer phone number"] = "",
    ) -> str:
        """Create Stripe customer via Cloud Function."""
        payload = {"email": email}
        if name:
            payload["name"] = name
        if phone:
            payload["phone"] = phone

        result = await self._call_function("createStripeCustomer", payload)

        if result["success"]:
            return f"Stripe customer created: {result['data']}"
        return f"Failed to create Stripe customer: {result['error']}"

    @kernel_function(
        name="get_agent_crm_products",
        description="Get all products formatted for CRM/agent use including courses, workshops, and services with pricing."
    )
    async def get_agent_crm_products(self) -> str:
        """Get CRM products via Cloud Function - formatted for agent analysis."""
        result = await self._call_function("getAgentCRMProducts", {})

        if result["success"]:
            return str(result["data"])
        return f"Failed to get CRM products: {result['error']}"

    @kernel_function(
        name="get_tutorials",
        description="Get list of available tutorials and educational content."
    )
    async def get_tutorials(
        self,
        category: Annotated[str, "Filter by category (optional)"] = "",
        limit: Annotated[int, "Maximum number of tutorials to return"] = 20,
    ) -> str:
        """Get tutorials via Cloud Function."""
        payload: dict = {"limit": limit}
        if category:
            payload["category"] = category

        result = await self._call_function("getTutorials", payload)

        if result["success"]:
            return str(result["data"])
        return f"Failed to get tutorials: {result['error']}"

    @kernel_function(
        name="add_fcm_token",
        description="Register a Firebase Cloud Messaging token for push notifications."
    )
    async def add_fcm_token(
        self,
        user_id: Annotated[str, "User ID to associate with the token"],
        fcm_token: Annotated[str, "Firebase Cloud Messaging device token"],
        device_type: Annotated[str, "Device type (ios, android, web)"] = "web",
    ) -> str:
        """Register FCM token for push notifications."""
        result = await self._call_function(
            "addFcmToken",
            {
                "userId": user_id,
                "fcmToken": fcm_token,
                "deviceType": device_type,
            }
        )

        if result["success"]:
            return f"FCM token registered for user {user_id}"
        return f"Failed to register FCM token: {result['error']}"
