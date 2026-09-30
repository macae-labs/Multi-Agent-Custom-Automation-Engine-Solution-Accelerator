"""
Tests for utility functions.
"""

import pytest
from json import loads

from datetime import datetime
from utils.date_utils import (
    format_date_for_user,
    get_current_timestamp,
    format_timestamp_for_display,
)
from utils.formatters import (
    format_mcp_response,
    format_error_response,
    format_success_response,
)


class TestDateUtils:
    """Test cases for date utilities."""

    def test_format_date_for_user_standard_formats(self):
        """Test date formatting with standard formats."""
        # Test YYYY-MM-DD format
        result = format_date_for_user("2024-12-25")
        assert "December 25, 2024" in result

        # Test MM/DD/YYYY format
        result = format_date_for_user("12/25/2024")
        assert "December 25, 2024" in result

        # Test invalid format returns original
        result = format_date_for_user("invalid-date")
        assert result == "invalid-date"

    def test_get_current_timestamp(self):
        """Test current timestamp generation."""
        timestamp = get_current_timestamp()
        assert isinstance(timestamp, str)
        assert "T" in timestamp  # ISO format should contain T

        # Should be able to parse it back
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))

    def test_format_timestamp_for_display(self):
        """Test timestamp formatting for display."""
        # Test with None (current time)
        result = format_timestamp_for_display()
        assert "UTC" in result

        # Test with specific timestamp
        test_timestamp = "2024-12-25T10:30:00Z"
        result = format_timestamp_for_display(test_timestamp)
        assert "December 25, 2024" in result
        assert "10:30" in result
        assert "UTC" in result

        # Test with invalid timestamp
        result = format_timestamp_for_display("invalid")
        assert result == "invalid"


class TestFormatters:
    """Un solo envelope JSON para éxito y error: el consumidor lee ``status``,
    no adivina por el primer carácter ni por un título en markdown."""

    def test_format_mcp_response(self):
        payload = loads(
            format_mcp_response(
                "Test Action", {"user": "John", "status": "success"}, "Test completed successfully", "Do X"
            )
        )
        assert payload["status"] == "success"
        assert payload["action"] == "Test Action"
        assert payload["summary"] == "Test completed successfully"
        assert payload["details"]["user"] == "John"
        assert payload["details"]["instructions"] == "Do X"

    def test_format_error_response(self):
        payload = loads(format_error_response("Something went wrong", "testing error handling"))
        assert payload == {
            "status": "error",
            "action": "testing error handling",
            "summary": "Something went wrong",
        }

    def test_format_error_response_no_context(self):
        payload = loads(format_error_response("Something went wrong"))
        assert payload == {"status": "error", "summary": "Something went wrong"}

    def test_format_success_response(self):
        payload = loads(
            format_success_response(
                "User Creation", {"user": "John", "email": "john@example.com"}, "User created successfully"
            )
        )
        assert payload["status"] == "success"
        assert payload["action"] == "User Creation"
        assert payload["summary"] == "User created successfully"
        assert payload["details"] == {"user": "John", "email": "john@example.com"}

    def test_format_success_response_without_summary_has_no_summary_key(self):
        payload = loads(format_success_response("User Creation", {"user": "John"}))
        assert payload["status"] == "success"
        assert "summary" not in payload

    def test_success_and_error_share_one_envelope(self):
        ok = loads(format_success_response("a", {"k": 1}, "s"))
        ko = loads(format_error_response("m", "a"))
        assert set(ko) == {"status", "action", "summary"}
        assert set(ko) <= set(ok)
        assert (ok["status"], ko["status"]) == ("success", "error")
