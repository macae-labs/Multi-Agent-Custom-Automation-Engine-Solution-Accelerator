"""Unit tests for ValidatorAgent - specifically _normalize_tools() contract."""

import json
import pytest

from kernel_agents.validator_agent import ValidatorAgent


class TestValidatorAgentNormalizeTools:
    """Tests for ValidatorAgent._normalize_tools() contract.

    This function must:
    1. Parse tools with "function" key (local generate_tools_json_doc format)
    2. Parse tools with "name" key (Azure AI Agent format)
    3. Never return "unknown" when a valid name exists
    4. Limit output to max 30 tools per agent
    """

    @pytest.fixture
    def validator(self):
        """Create a ValidatorAgent with no planner (not needed for _normalize_tools)."""
        return ValidatorAgent(
            planner_agent=None,
            available_agents=["Tech_Support_Agent", "Hr_Agent", "Generic_Agent"],
        )

    # ========== Format: "function" key (local format) ==========

    def test_parses_function_key_format(self, validator):
        """_normalize_tools() must extract names from 'function' key (local format)."""
        tools_data = {
            "Tech_Support_Agent": json.dumps(
                [
                    {
                        "agent": "Tech_Support_Agent",
                        "function": "send_welcome_email",
                        "description": "Send email",
                    },
                    {
                        "agent": "Tech_Support_Agent",
                        "function": "configure_laptop",
                        "description": "Configure laptop",
                    },
                ]
            )
        }

        result = validator._normalize_tools(tools_data)

        assert "send_welcome_email" in result
        assert "configure_laptop" in result
        assert "- Tech_Support_Agent:" in result

    def test_parses_multiple_agents_function_format(self, validator):
        """_normalize_tools() must handle multiple agents with 'function' format."""
        tools_data = {
            "Tech_Support_Agent": json.dumps(
                [
                    {"function": "send_welcome_email"},
                    {"function": "reset_password"},
                ]
            ),
            "Hr_Agent": json.dumps(
                [
                    {"function": "onboard_employee"},
                    {"function": "process_leave_request"},
                ]
            ),
        }

        result = validator._normalize_tools(tools_data)

        assert "- Tech_Support_Agent: send_welcome_email, reset_password" in result
        assert "- Hr_Agent: onboard_employee, process_leave_request" in result

    # ========== Format: "name" key (Azure format) ==========

    def test_parses_name_key_format(self, validator):
        """_normalize_tools() must extract names from 'name' key (Azure format)."""
        tools_data = {
            "Tech_Support_Agent": json.dumps(
                [
                    {"name": "send_welcome_email", "description": "Send welcome email"},
                    {"name": "configure_laptop", "description": "Configure laptop"},
                ]
            )
        }

        result = validator._normalize_tools(tools_data)

        assert "send_welcome_email" in result
        assert "configure_laptop" in result

    def test_prefers_name_over_function_when_both_present(self, validator):
        """When both 'name' and 'function' exist, 'name' takes precedence."""
        tools_data = {
            "Agent": json.dumps(
                [
                    {"name": "correct_name", "function": "wrong_name"},
                ]
            )
        }

        result = validator._normalize_tools(tools_data)

        assert "correct_name" in result
        assert "wrong_name" not in result

    # ========== Contract: No "unknown" when valid name exists ==========

    def test_no_unknown_with_valid_function_name(self, validator):
        """Must NOT return 'unknown' when 'function' key has valid value."""
        tools_data = {
            "Tech_Support_Agent": json.dumps(
                [
                    {"function": "valid_tool_name"},
                ]
            )
        }

        result = validator._normalize_tools(tools_data)

        assert "unknown" not in result.lower()
        assert "valid_tool_name" in result

    def test_no_unknown_with_valid_name_key(self, validator):
        """Must NOT return 'unknown' when 'name' key has valid value."""
        tools_data = {
            "Tech_Support_Agent": json.dumps(
                [
                    {"name": "valid_tool_name"},
                ]
            )
        }

        result = validator._normalize_tools(tools_data)

        assert "unknown" not in result.lower()
        assert "valid_tool_name" in result

    def test_unknown_only_when_no_name_or_function(self, validator):
        """'unknown' should only appear when neither 'name' nor 'function' exists."""
        tools_data = {
            "Agent": json.dumps(
                [
                    {
                        "description": "A tool with no name"
                    },  # Missing both name and function
                ]
            )
        }

        result = validator._normalize_tools(tools_data)

        # This case SHOULD produce 'unknown' because there's no name
        assert "unknown" in result.lower()

    # ========== Contract: Output limiting (max 30 tools) ==========

    def test_limits_output_to_30_tools(self, validator):
        """_normalize_tools() must limit output to max 30 tools per agent."""
        # Generate 50 tools
        many_tools = [{"function": f"tool_{i}"} for i in range(50)]
        tools_data = {"Agent": json.dumps(many_tools)}

        result = validator._normalize_tools(tools_data)

        # Should have tool_0 through tool_29
        assert "tool_0" in result
        assert "tool_29" in result
        # Should NOT have tool_30 or beyond in the list
        assert "tool_30," not in result
        # Should indicate there are more
        assert "...+20 more" in result

    def test_no_truncation_indicator_when_under_limit(self, validator):
        """No '...+X more' when tools count is <= 30."""
        tools = [{"function": f"tool_{i}"} for i in range(25)]
        tools_data = {"Agent": json.dumps(tools)}

        result = validator._normalize_tools(tools_data)

        assert "...+" not in result
        assert "more" not in result

    def test_exactly_30_tools_no_truncation(self, validator):
        """Exactly 30 tools should not show truncation indicator."""
        tools = [{"function": f"tool_{i}"} for i in range(30)]
        tools_data = {"Agent": json.dumps(tools)}

        result = validator._normalize_tools(tools_data)

        assert "tool_29" in result
        assert "...+" not in result

    # ========== Edge cases ==========

    def test_handles_empty_tools_list(self, validator):
        """Must handle empty tools list gracefully."""
        tools_data = {"Agent": "[]"}

        result = validator._normalize_tools(tools_data)

        # Should have the agent but with empty tools
        assert "- Agent:" in result

    def test_handles_invalid_json(self, validator):
        """Must handle invalid JSON gracefully (fail-open)."""
        tools_data = {"Agent": "not valid json {{{"}

        result = validator._normalize_tools(tools_data)

        # Should not crash, should indicate tools available
        assert "- Agent:" in result
        assert "(tools available)" in result

    def test_handles_non_list_json(self, validator):
        """Must handle non-list JSON (e.g., object) gracefully."""
        tools_data = {"Agent": json.dumps({"not": "a list"})}

        result = validator._normalize_tools(tools_data)

        assert "- Agent:" in result
        assert "(see full doc)" in result

    def test_handles_mixed_valid_invalid_entries(self, validator):
        """Must extract valid tools even when some entries are malformed."""
        tools_data = {
            "Agent": json.dumps(
                [
                    {"function": "valid_tool"},
                    "not a dict",  # Invalid
                    {"function": "another_valid"},
                    123,  # Invalid
                ]
            )
        }

        result = validator._normalize_tools(tools_data)

        assert "valid_tool" in result
        assert "another_valid" in result


class TestValidatorAgentIntegration:
    """Integration tests ensuring _normalize_tools works with real tool generators."""

    def test_works_with_tech_support_tools_format(self):
        """Verify compatibility with TechSupportTools.generate_tools_json_doc() format."""
        from kernel_tools.tech_support_tools import TechSupportTools

        validator = ValidatorAgent(None, ["Tech_Support_Agent"])
        tools_data = {"Tech_Support_Agent": TechSupportTools.generate_tools_json_doc()}

        result = validator._normalize_tools(tools_data)

        # Must include real tool names, not "unknown"
        assert "send_welcome_email" in result
        assert "- Tech_Support_Agent:" in result
        # Verify no unknown in the tools list part
        tools_part = result.split(":")[1] if ":" in result else ""
        assert "unknown" not in tools_part.lower()

    def test_works_with_hr_tools_format(self):
        """Verify compatibility with HrTools.generate_tools_json_doc() format."""
        from kernel_tools.hr_tools import HrTools

        validator = ValidatorAgent(None, ["Hr_Agent"])
        tools_data = {"Hr_Agent": HrTools.generate_tools_json_doc()}

        result = validator._normalize_tools(tools_data)

        # Must include real HR tool names
        assert "- Hr_Agent:" in result
        # Should have actual function names, not all unknowns
        lines = result.split("\n")
        hr_line = [line for line in lines if "Hr_Agent" in line][0]
        tools_part = hr_line.split(":")[1] if ":" in hr_line else ""
        assert "unknown" not in tools_part.lower()

    def test_works_with_all_agent_tools(self):
        """Verify _normalize_tools works with all agent tool generators."""
        from kernel_tools.tech_support_tools import TechSupportTools
        from kernel_tools.hr_tools import HrTools
        from kernel_tools.marketing_tools import MarketingTools
        from kernel_tools.product_tools import ProductTools
        from kernel_tools.procurement_tools import ProcurementTools
        from kernel_tools.generic_tools import GenericTools

        validator = ValidatorAgent(
            None,
            [
                "Tech_Support_Agent",
                "Hr_Agent",
                "Marketing_Agent",
                "Product_Agent",
                "Procurement_Agent",
                "Generic_Agent",
            ],
        )

        tools_data = {
            "Tech_Support_Agent": TechSupportTools.generate_tools_json_doc(),
            "Hr_Agent": HrTools.generate_tools_json_doc(),
            "Marketing_Agent": MarketingTools.generate_tools_json_doc(),
            "Product_Agent": ProductTools.generate_tools_json_doc(),
            "Procurement_Agent": ProcurementTools.generate_tools_json_doc(),
            "Generic_Agent": GenericTools.generate_tools_json_doc(),
        }

        result = validator._normalize_tools(tools_data)

        # Each agent should have a line
        assert "- Tech_Support_Agent:" in result
        assert "- Hr_Agent:" in result
        assert "- Marketing_Agent:" in result
        assert "- Product_Agent:" in result
        assert "- Procurement_Agent:" in result
        assert "- Generic_Agent:" in result

        # Count "unknown" occurrences - should be minimal (only for truly unnamed tools)
        unknown_count = result.lower().count("unknown")
        assert unknown_count == 0, (
            f"Found {unknown_count} 'unknown' entries - tools not being parsed correctly"
        )
