"""
Response formatting utilities for MCP tools.
"""

from typing import Dict, Any, Optional


def format_mcp_response(
    title: str,
    content: Dict[str, Any],
    agent_summary: str,
    additional_instructions: Optional[str] = None,
) -> str:
    """
    Format a standardized MCP response.

    Args:
        title: The title of the response section
        content: Dictionary of content to display
        agent_summary: Summary of what the agent did
        additional_instructions: Optional additional formatting instructions

    Returns:
        Formatted markdown response
    """
    response_parts = [f"##### {title}\n"]

    # Add content fields
    for key, value in content.items():
        formatted_key = key.replace("_", " ").title()
        response_parts.append(f"**{formatted_key}:** {value}")

    response_parts.append("")  # Empty line
    response_parts.append(f"AGENT SUMMARY: {agent_summary}")

    return "\n".join(response_parts)


def format_error_response(error_message: str, context: Optional[str] = None) -> str:
    """
    Format an error response for MCP tools.

    Args:
        error_message: The error message to display
        context: Optional context about when the error occurred

    Returns:
        Formatted error response
    """
    response_parts = ["##### ❌ Error\n"]

    if context:
        response_parts.append(f"**Context:** {context}")

    response_parts.append(f"**Error:** {error_message}")
    response_parts.append("")
    response_parts.append(
        "AGENT SUMMARY: An error occurred while processing the request."
    )

    return "\n".join(response_parts)


def format_success_response(
    action: str, details: Dict[str, Any], summary: Optional[str] = None
) -> str:
    """
    Format a success response for MCP tools.

    Args:
        action: The action that was performed
        details: Details about the action
        summary: Optional custom summary

    Returns:
        Formatted success response - returns the actual result directly
    """
    # Return the actual result directly, not fancy markdown
    return details.get("result", f"Action completed successfully: {action}")
