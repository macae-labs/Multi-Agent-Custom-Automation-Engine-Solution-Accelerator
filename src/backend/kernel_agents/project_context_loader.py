"""Project context loader for multi-tenant plugin injection."""

import inspect
import logging
from typing import Any, Dict, List, Optional

from context.cosmos_memory_kernel import CosmosMemoryContext
from models.project_profile import ProjectProfile
from kernel_tools.external_api_plugin import ExternalAPIPlugin
from kernel_tools.s3_plugin import S3Plugin
from kernel_tools.firestore_plugin import FirestorePlugin
from kernel_tools.cloud_functions_plugin import CloudFunctionsPlugin
from semantic_kernel.functions import KernelFunction
from tool_registry import ToolRegistry


class ProjectContextLoader:
    """Load project-specific plugins based on profile."""

    @staticmethod
    def _create_plugin_instance(
        provider_id: str,
        profile: ProjectProfile,
        session_id: str,
        user_id: str,
        custom_config: Dict[str, Any],
    ) -> Optional[Any]:
        if provider_id == "aws_s3":
            return S3Plugin(
                project_id=profile.project_id,
                session_id=session_id,
                user_id=user_id,
            )
        if provider_id == "firestore":
            return FirestorePlugin(
                project_id=custom_config.get("gcp_project_id", profile.project_id),
                collection_root=profile.firestore_root or "",
                session_id=session_id,
                user_id=user_id,
            )
        if provider_id == "cloud_functions":
            return CloudFunctionsPlugin(
                project_id=custom_config.get("gcp_project_id", profile.project_id),
                session_id=session_id,
                user_id=user_id,
            )
        return None

    @staticmethod
    def _discover_kernel_methods(plugin_instance: Any) -> Dict[str, Any]:
        """Discover @kernel_function methods and index them by tool_id."""
        discovered: Dict[str, Any] = {}
        for _name, member in inspect.getmembers(plugin_instance, predicate=callable):
            if getattr(member, "__kernel_function__", False):
                tool_id = getattr(member, "__kernel_function_name__", None)
                if tool_id:
                    discovered[str(tool_id)] = member
        return discovered

    @staticmethod
    async def load_project_profile(
        memory_store: CosmosMemoryContext,
        session_id: str
    ) -> Optional[ProjectProfile]:
        """Load project profile from Cosmos DB."""
        try:
            profile_data = await memory_store.get_data_by_type_and_session_id(
                "project_profile",
                session_id
            )
            if profile_data:
                return ProjectProfile.model_validate(profile_data[-1])
            fallback_profile = await memory_store.get_latest_data_by_type_for_user(
                "project_profile"
            )
            if fallback_profile:
                logging.info(
                    "No project profile found for session %s; using latest profile for user fallback.",
                    session_id,
                )
                return ProjectProfile.model_validate(fallback_profile)
        except Exception as e:
            logging.warning(f"No project profile found: {e}")
        return None

    @staticmethod
    def create_plugins_from_profile(
        profile: ProjectProfile,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_type: Optional[str] = None,
    ) -> List[KernelFunction]:
        """Create plugins based on project profile."""
        plugins: List[KernelFunction] = []
        enabled_tools = set(profile.enabled_tools or [])
        custom_config = profile.custom_config or {}
        effective_session_id = session_id or profile.session_id
        effective_user_id = user_id or profile.user_id
        active_providers = {
            binding.provider_id
            for binding in (profile.credential_bindings or [])
            if getattr(binding, "is_active", False)
        }

        # External API plugin
        if profile.api_base_url and (
            not enabled_tools or "external_api" in enabled_tools
        ):
            api_plugin = ExternalAPIPlugin(
                base_url=profile.api_base_url,
                api_key=profile.api_key or ""
            )
            plugins.append(KernelFunction.from_method(api_plugin.call_api))

        # Declarative tool resolution by agent + project policy.
        resolved_agent_type = agent_type or "Tech_Support_Agent"
        tool_defs = ToolRegistry.get_tools_for_agent_and_profile(
            agent_type=resolved_agent_type,
            enabled_tools=list(enabled_tools),
            active_providers=sorted(active_providers),
        )
        allowed_tool_ids = {tool.tool_id for tool in tool_defs}
        providers = sorted({tool.provider_id for tool in tool_defs})

        for provider_id in providers:
            plugin_instance = ProjectContextLoader._create_plugin_instance(
                provider_id=provider_id,
                profile=profile,
                session_id=effective_session_id,
                user_id=effective_user_id,
                custom_config=custom_config,
            )
            if plugin_instance is None:
                continue

            discovered = ProjectContextLoader._discover_kernel_methods(plugin_instance)
            for tool_id in sorted(allowed_tool_ids):
                method = discovered.get(tool_id)
                if method is None:
                    continue
                plugins.append(KernelFunction.from_method(method))

        loaded_tool_names: List[str] = []
        for plugin in plugins:
            metadata = getattr(plugin, "metadata", None)
            tool_name = (
                getattr(metadata, "name", None)
                or getattr(plugin, "name", None)
                or getattr(plugin, "function_name", None)
            )
            if tool_name:
                loaded_tool_names.append(str(tool_name))

        logging.info(
            "Loaded %s project plugins for project %s (active providers: %s): %s",
            len(plugins),
            profile.project_id,
            sorted(active_providers),
            sorted(set(loaded_tool_names)),
        )
        return plugins

    @staticmethod
    def create_fallback_plugins(
        session_id: str,
        user_id: str,
        project_id: str = "default",
        agent_type: str = "Tech_Support_Agent",
    ) -> List[KernelFunction]:
        """Create a minimal fallback plugin set when no project profile is found.

        This prevents "function not available" failures and allows tools to
        return structured `credentials_required` responses.
        """
        fallback_profile = ProjectProfile(
            session_id=session_id,
            user_id=user_id,
            project_id=project_id,
            project_name="fallback",
            api_base_url=None,
            aws_s3_bucket=None,
            firestore_root="",
            enabled_tools=[],
            api_key=None,
            custom_config={},
            credential_bindings=[],
        )
        plugins: List[KernelFunction] = ProjectContextLoader.create_plugins_from_profile(
            fallback_profile,
            session_id=session_id,
            user_id=user_id,
            agent_type=agent_type,
        )
        logging.info(
            "Loaded fallback project plugins for session %s: %s",
            session_id,
            sorted(set(
                str(getattr(getattr(p, "metadata", None), "name", None) or getattr(p, "name", ""))
                for p in plugins
            )),
        )
        return plugins
