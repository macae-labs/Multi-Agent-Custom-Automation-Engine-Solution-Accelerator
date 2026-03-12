import locale
from datetime import datetime, timedelta
import logging
import re
from typing import Optional


def format_date_for_user(date_str: str, user_locale: Optional[str] = None) -> str:
    """
    Format date based on user's desktop locale preference.

    Args:
        date_str (str): Date in ISO format (YYYY-MM-DD).
        user_locale (str, optional): User's locale string, e.g., 'en_US', 'en_GB'.

    Returns:
        str: Formatted date respecting locale or raw date if formatting fails.
    """
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        locale.setlocale(locale.LC_TIME, user_locale or "")
        return date_obj.strftime("%B %d, %Y")
    except Exception as e:
        logging.warning(f"Date formatting failed for '{date_str}': {e}")
        return date_str


def parse_date_string(date_str: str) -> datetime:
    """
    Parse a date string into a datetime object.

    Supports various formats:
    - ISO format: "2024-02-15", "2024-02-15T10:00:00"
    - Natural language: "next Monday", "tomorrow", "in 3 days"
    - Common formats: "02/15/2024", "February 15, 2024"

    Args:
        date_str: The date string to parse

    Returns:
        datetime: Parsed datetime object (defaults to 10:00 AM if no time specified)
    """
    # Default time for scheduled events
    default_hour = 10
    default_minute = 0

    date_str = date_str.strip().lower()
    today = datetime.now().replace(
        hour=default_hour, minute=default_minute, second=0, microsecond=0
    )

    # Handle relative dates
    if date_str == "today":
        return today
    elif date_str == "tomorrow":
        return today + timedelta(days=1)
    elif date_str == "yesterday":
        return today - timedelta(days=1)

    # Handle "in X days"
    in_days_match = re.match(r"in\s+(\d+)\s+days?", date_str)
    if in_days_match:
        days = int(in_days_match.group(1))
        return today + timedelta(days=days)

    # Handle "next Monday", "next Tuesday", etc.
    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    next_day_match = re.match(r"next\s+(\w+)", date_str)
    if next_day_match:
        day_name = next_day_match.group(1)
        if day_name in weekdays:
            target_weekday = weekdays[day_name]
            current_weekday = today.weekday()
            days_ahead = target_weekday - current_weekday
            if days_ahead <= 0:
                days_ahead += 7
            return today + timedelta(days=days_ahead)

    # Try common date formats
    formats = [
        "%Y-%m-%d",  # 2024-02-15
        "%Y-%m-%dT%H:%M:%S",  # 2024-02-15T10:00:00
        "%Y-%m-%dT%H:%M",  # 2024-02-15T10:00
        "%m/%d/%Y",  # 02/15/2024
        "%d/%m/%Y",  # 15/02/2024
        "%B %d, %Y",  # February 15, 2024
        "%b %d, %Y",  # Feb 15, 2024
        "%d %B %Y",  # 15 February 2024
        "%Y/%m/%d",  # 2024/02/15
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(date_str, fmt)
            # If no time was specified, set to default time
            if parsed.hour == 0 and parsed.minute == 0:
                parsed = parsed.replace(hour=default_hour, minute=default_minute)
            return parsed
        except ValueError:
            continue

    # If all else fails, try to parse with dateutil if available
    try:
        from dateutil import parser

        parsed = parser.parse(date_str)
        if parsed.hour == 0 and parsed.minute == 0:
            parsed = parsed.replace(hour=default_hour, minute=default_minute)
        return parsed
    except (ImportError, ValueError):
        pass

    # Last resort: return today + 7 days with a warning
    logging.warning(
        f"Could not parse date '{date_str}', defaulting to one week from today"
    )
    return today + timedelta(days=7)
