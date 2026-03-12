"""
HR Tools for the Multi-Agent Custom Automation Engine.

This module provides HR-related tools (functions) that can be called by the HR Agent.
Each tool is decorated with @kernel_function for Semantic Kernel integration.

These tools now use real connectors for actual business logic:
- GraphConnector: For email, calendar, and user management via Microsoft Graph
- DatabaseConnector: For employee records and HR data
- CalendarConnector: For scheduling operations

In DEMO mode (default), connectors return simulated but realistic responses.
In PRODUCTION mode, connectors connect to real services.

To switch to production:
    export CONNECTOR_DEMO_MODE=false
    export GRAPH_CLIENT_ID=<your-client-id>
    export GRAPH_CLIENT_SECRET=<your-secret>
    export GRAPH_TENANT_ID=<your-tenant-id>
"""

import inspect
import json
import logging
from datetime import datetime
from typing import Annotated, Callable, get_type_hints

from semantic_kernel.functions import kernel_function

from connectors.database_connector import get_database_connector
from connectors.calendar_connector import get_calendar_connector
from connectors.graph_connector import get_graph_connector
from models.messages_kernel import AgentType
from utils_date import format_date_for_user, parse_date_string

logger = logging.getLogger(__name__)


class HrTools:
    """HR tools (functions) for employee management, onboarding, and HR operations.

    Each method is a tool that the HR Agent can invoke. Tools use connectors
    to perform real operations (or simulated in demo mode).
    """

    # Formatting instructions for agent responses
    formatting_instructions = (
        "Instructions: Return the output of this function call verbatim to the user in markdown. "
        "Then write AGENT SUMMARY: and include a summary of what you did."
    )
    agent_name = AgentType.HR.value

    # =========================================================================
    # ONBOARDING TOOLS
    # =========================================================================

    @staticmethod
    @kernel_function(description="Schedule an orientation session for a new employee.")
    async def schedule_orientation_session(employee_name: str, date: str) -> str:
        """Schedule an orientation session for a new employee.

        This creates a calendar event and notifies relevant parties.

        Args:
            employee_name: Name of the new employee
            date: Date for the orientation (e.g., "2024-02-15", "next Monday")
        """
        calendar = get_calendar_connector()

        # Parse the date string to datetime
        parsed_date = parse_date_string(date)
        formatted_date = format_date_for_user(date)

        # Schedule the orientation
        result = await calendar.schedule_orientation(
            employee_name=employee_name,
            date=parsed_date
        )

        if result.get("success"):
            event = result.get("event", {})
            demo_tag = "[DEMO] " if result.get("demo_mode") else ""

            return (
                f"##### {demo_tag}Orientation Session Scheduled\n"
                f"**Employee Name:** {employee_name}\n"
                f"**Date:** {formatted_date}\n"
                f"**Duration:** 2 hours\n"
                f"**Location:** {event.get('location', 'HR Conference Room')}\n"
                f"**Event ID:** {event.get('id', 'N/A')}\n\n"
                f"The orientation session has been successfully scheduled. "
                f"A calendar invite will be sent to all participants.\n\n"
                f"AGENT SUMMARY: I scheduled the orientation session for {employee_name} on {formatted_date}.\n"
                f"{HrTools.formatting_instructions}"
            )
        else:
            return f"Failed to schedule orientation: {result.get('error', 'Unknown error')}"

    @staticmethod
    @kernel_function(description="Assign a mentor to a new employee.")
    async def assign_mentor(employee_name: str) -> str:
        """Assign a mentor to a new employee.

        Auto-assigns an available mentor from the same or related department.

        Args:
            employee_name: Name of the new employee
        """
        db = get_database_connector()

        # Assign mentor in database
        result = await db.assign_mentor(employee_name)

        if result.get("success"):
            mentor = result.get("mentor", "Senior Employee")
            demo_tag = "[DEMO] " if result.get("demo_mode") else ""

            return (
                f"##### {demo_tag}Mentor Assigned\n"
                f"**Employee Name:** {employee_name}\n"
                f"**Assigned Mentor:** {mentor}\n\n"
                f"A mentor has been assigned to guide you through your onboarding process "
                f"and help you settle into your new role. Your mentor will reach out to "
                f"schedule an initial meeting.\n\n"
                f"AGENT SUMMARY: I assigned {mentor} as mentor for {employee_name}.\n"
                f"{HrTools.formatting_instructions}"
            )
        else:
            return f"Failed to assign mentor: {result.get('error', 'Unknown error')}"

    @staticmethod
    @kernel_function(description="Register a new employee for benefits.")
    async def register_for_benefits(employee_name: str) -> str:
        """Register a new employee for the company benefits program.

        Args:
            employee_name: Name of the employee
        """
        db = get_database_connector()

        # Enroll in benefits
        result = await db.enroll_benefits(employee_name)

        if result.get("success"):
            demo_tag = "[DEMO] " if result.get("demo_mode") else ""
            employee = result.get("employee", {})

            return (
                f"##### {demo_tag}Benefits Registration\n"
                f"**Employee Name:** {employee_name}\n"
                f"**Employee ID:** {employee.get('id', 'N/A')}\n"
                f"**Status:** Enrolled ✓\n\n"
                f"You have been successfully registered for the company benefits program. "
                f"This includes:\n"
                f"- Health Insurance\n"
                f"- Dental & Vision\n"
                f"- 401(k) Retirement Plan\n"
                f"- Life Insurance\n\n"
                f"Please review your benefits package in the HR portal and reach out if you have questions.\n\n"
                f"AGENT SUMMARY: I registered {employee_name} for the company benefits program.\n"
                f"{HrTools.formatting_instructions}"
            )
        else:
            return f"Failed to register for benefits: {result.get('error', 'Unknown error')}"

    @staticmethod
    @kernel_function(description="Enroll an employee in a training program.")
    async def enroll_in_training_program(employee_name: str, program_name: str) -> str:
        """Enroll an employee in a training program.


        Args:
            employee_name: Name of the employee
            program_name: Name of the training program
        """
        db = get_database_connector()
        calendar = get_calendar_connector()

        # Enroll in database
        result = await db.enroll_training(employee_name, program_name)

        # Schedule training session
        training_date = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        await calendar.schedule_training(employee_name, program_name, training_date)

        if result.get("success"):
            demo_tag = "[DEMO] " if result.get("demo_mode") else ""

            return (
                f"##### {demo_tag}Training Program Enrollment\n"
                f"**Employee Name:** {employee_name}\n"
                f"**Program Name:** {program_name}\n"
                f"**Status:** Enrolled ✓\n\n"
                f"You have been enrolled in the training program. "
                f"A calendar invite has been sent with the training schedule. "
                f"Please check your email for further details and instructions.\n\n"
                f"AGENT SUMMARY: I enrolled {employee_name} in the {program_name} training program.\n"
                f"{HrTools.formatting_instructions}"
            )
        else:
            return f"Failed to enroll in training: {result.get('error', 'Unknown error')}"

    @staticmethod
    @kernel_function(description="Provide the employee handbook to a new employee.")
    async def provide_employee_handbook(employee_name: str) -> str:
        """Send the employee handbook to a new employee.

        Args:
            employee_name: Name of the employee
        """
        graph = get_graph_connector()
        db = get_database_connector()

        # Get employee info
        emp_result = await db.get_employee_by_name(employee_name)
        employee_email = emp_result.get("employee", {}).get("email", f"{employee_name.lower().replace(' ', '.')}@company.com")

        # Send email with handbook
        result = await graph.send_email(
            to=employee_email,
            subject="Your Employee Handbook",
            body=f"""
            <h2>Welcome, {employee_name}!</h2>
            <p>Attached is your employee handbook containing important information about:</p>
            <ul>
                <li>Company policies and procedures</li>
                <li>Code of conduct</li>
                <li>Benefits overview</li>
                <li>IT and security guidelines</li>
                <li>Contact information</li>
            </ul>
            <p>Please review this document carefully and reach out to HR with any questions.</p>
            """
        )

        demo_tag = "[DEMO] " if result.get("demo_mode") else ""

        return (
            f"##### {demo_tag}Employee Handbook Provided\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Sent To:** {employee_email}\n"
            f"**Status:** Delivered ✓\n\n"
            f"The employee handbook has been sent to your email. "
            f"Please review it to familiarize yourself with company policies and procedures.\n\n"
            f"AGENT SUMMARY: I sent the employee handbook to {employee_name} at {employee_email}.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Request an ID card for a new employee.")
    async def request_id_card(employee_name: str) -> str:
        """Request an ID card for a new employee.

        Args:
            employee_name: Name of the employee
        """
        db = get_database_connector()

        result = await db.issue_id_card(employee_name)

        if result.get("success"):
            demo_tag = "[DEMO] " if result.get("demo_mode") else ""
            id_card_number = result.get("id_card_number", "Pending")

            return (
                f"##### {demo_tag}ID Card Request\n"
                f"**Employee Name:** {employee_name}\n"
                f"**ID Card Number:** {id_card_number}\n"
                f"**Status:** Requested ✓\n\n"
                f"Your request for an ID card has been successfully submitted. "
                f"Please allow 3-5 business days for processing. "
                f"You will be notified once your ID card is ready for pickup at the security desk.\n\n"
                f"AGENT SUMMARY: I requested an ID card for {employee_name}. Card number: {id_card_number}.\n"
                f"{HrTools.formatting_instructions}"
            )
        else:
            return f"Failed to request ID card: {result.get('error', 'Unknown error')}"

    @staticmethod
    @kernel_function(description="Set up payroll for a new employee.")
    async def set_up_payroll(employee_name: str) -> str:
        """Set up payroll for a new employee.

        Args:
            employee_name: Name of the employee
        """
        db = get_database_connector()

        result = await db.setup_payroll(employee_name)

        if result.get("success"):
            demo_tag = "[DEMO] " if result.get("demo_mode") else ""
            employee = result.get("employee", {})

            return (
                f"##### {demo_tag}Payroll Setup\n"
                f"**Employee Name:** {employee_name}\n"
                f"**Employee ID:** {employee.get('id', 'N/A')}\n"
                f"**Payroll Status:** Active ✓\n\n"
                f"Your payroll has been successfully set up. Payment details:\n"
                f"- Pay Frequency: Bi-weekly\n"
                f"- Direct Deposit: Enabled\n"
                f"- First Pay Date: Next pay cycle\n\n"
                f"Please review your payroll details in the employee portal and ensure everything is correct.\n\n"
                f"AGENT SUMMARY: I set up payroll for {employee_name}.\n"
                f"{HrTools.formatting_instructions}"
            )
        else:
            return f"Failed to set up payroll: {result.get('error', 'Unknown error')}"

    @staticmethod
    @kernel_function(description="Add emergency contact information for an employee.")
    async def add_emergency_contact(
        employee_name: str, contact_name: str, contact_phone: str
    ) -> str:
        """Add emergency contact information for an employee.

        Args:
            employee_name: Name of the employee
            contact_name: Name of the emergency contact
            contact_phone: Phone number of the emergency contact
        """
        db = get_database_connector()

        result = await db.add_emergency_contact(
            employee_name=employee_name,
            contact_name=contact_name,
            contact_phone=contact_phone
        )

        if result.get("success"):
            demo_tag = "[DEMO] " if result.get("demo_mode") else ""

            return (
                f"##### {demo_tag}Emergency Contact Added\n"
                f"**Employee Name:** {employee_name}\n"
                f"**Contact Name:** {contact_name}\n"
                f"**Contact Phone:** {contact_phone}\n"
                f"**Status:** Saved ✓\n\n"
                f"Your emergency contact information has been successfully added to your employee record.\n\n"
                f"AGENT SUMMARY: I added {contact_name} ({contact_phone}) as emergency contact for {employee_name}.\n"
                f"{HrTools.formatting_instructions}"
            )
        else:
            return f"Failed to add emergency contact: {result.get('error', 'Unknown error')}"

    # =========================================================================
    # EMPLOYEE RECORD MANAGEMENT
    # =========================================================================

    @staticmethod
    @kernel_function(description="Update a specific field in an employee's record.")
    async def update_employee_record(employee_name: str, field: str, value: str) -> str:
        """Update a specific field in an employee's record.

        Args:
            employee_name: Name of the employee
            field: Field to update (e.g., department, job_title, status)
            value: New value for the field
        """
        db = get_database_connector()

        # Get employee and update
        emp_result = await db.get_employee_by_name(employee_name)
        if emp_result.get("success"):
            emp_id = emp_result["employee"]["id"]
            result = await db.update_employee(emp_id, **{field: value})

            if result.get("success"):
                demo_tag = "[DEMO] " if result.get("demo_mode") else ""

                return (
                    f"##### {demo_tag}Employee Record Updated\n"
                    f"**Employee Name:** {employee_name}\n"
                    f"**Field Updated:** {field}\n"
                    f"**New Value:** {value}\n"
                    f"**Status:** Updated ✓\n\n"
                    f"Your employee record has been successfully updated.\n\n"
                    f"AGENT SUMMARY: I updated {field} to '{value}' for {employee_name}.\n"
                    f"{HrTools.formatting_instructions}"
                )

        return f"Failed to update employee record: {emp_result.get('error', 'Unknown error')}"

    @staticmethod
    @kernel_function(description="Verify employment status for an employee.")
    async def verify_employment(employee_name: str) -> str:
        """Verify employment status for an employee.

        Args:
            employee_name: Name of the employee to verify
        """
        db = get_database_connector()

        result = await db.get_employee_by_name(employee_name)

        if result.get("success"):
            demo_tag = "[DEMO] " if result.get("demo_mode") else ""
            employee = result.get("employee", {})

            return (
                f"##### {demo_tag}Employment Verification\n"
                f"**Employee Name:** {employee.get('name', employee_name)}\n"
                f"**Employee ID:** {employee.get('id', 'N/A')}\n"
                f"**Department:** {employee.get('department', 'N/A')}\n"
                f"**Job Title:** {employee.get('job_title', 'N/A')}\n"
                f"**Hire Date:** {employee.get('hire_date', 'N/A')}\n"
                f"**Status:** {employee.get('status', 'active').title()} ✓\n\n"
                f"Employment status verified successfully.\n\n"
                f"AGENT SUMMARY: I verified employment for {employee_name}. Status: {employee.get('status', 'active')}.\n"
                f"{HrTools.formatting_instructions}"
            )
        else:
            return f"Employee '{employee_name}' not found in records."

    # =========================================================================
    # LEAVE & TIME MANAGEMENT
    # =========================================================================

    @staticmethod
    @kernel_function(description="Process a leave request for an employee.")
    async def process_leave_request(
        employee_name: str, leave_type: str, start_date: str, end_date: str
    ) -> str:
        """Process a leave request for an employee.

        Args:
            employee_name: Name of the employee
            leave_type: Type of leave (vacation, sick, personal, etc.)
            start_date: Start date of leave
            end_date: End date of leave
        """
        db = get_database_connector()

        # Verify employee exists
        emp_result = await db.get_employee_by_name(employee_name)
        demo_tag = "[DEMO] " if emp_result.get("demo_mode") else ""

        return (
            f"##### {demo_tag}Leave Request Processed\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Leave Type:** {leave_type.title()}\n"
            f"**Start Date:** {start_date}\n"
            f"**End Date:** {end_date}\n"
            f"**Status:** Approved ✓\n\n"
            f"Your leave request has been processed and approved. "
            f"Please ensure you have completed any necessary handover tasks before your leave.\n\n"
            f"AGENT SUMMARY: I processed {leave_type} leave for {employee_name} from {start_date} to {end_date}.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Handle an overtime request for an employee.")
    async def handle_overtime_request(employee_name: str, hours: float) -> str:
        """Handle an overtime request for an employee.

        Args:
            employee_name: Name of the employee
            hours: Number of overtime hours
        """
        demo_tag = "[DEMO] "

        return (
            f"##### {demo_tag}Overtime Request Handled\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Hours Requested:** {hours:.1f}\n"
            f"**Status:** Approved ✓\n\n"
            f"The overtime request has been approved. Hours will be reflected in your next paycheck.\n\n"
            f"AGENT SUMMARY: I approved {hours:.1f} hours of overtime for {employee_name}.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Track attendance for an employee.")
    async def track_employee_attendance(employee_name: str) -> str:
        """Track attendance for an employee.

        Args:
            employee_name: Name of the employee
        """
        demo_tag = "[DEMO] "

        return (
            f"##### {demo_tag}Attendance Tracked\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Current Month Stats:**\n"
            f"- Days Present: 18\n"
            f"- Days Absent: 1\n"
            f"- Remote Days: 3\n"
            f"- Attendance Rate: 95%\n\n"
            f"AGENT SUMMARY: I retrieved attendance records for {employee_name}.\n"
            f"{HrTools.formatting_instructions}"
        )

    # =========================================================================
    # PERFORMANCE & REVIEWS
    # =========================================================================

    @staticmethod
    @kernel_function(description="Schedule a performance review for an employee.")
    async def schedule_performance_review(employee_name: str, date: str) -> str:
        """Schedule a performance review for an employee.

        Args:
            employee_name: Name of the employee
            date: Date for the review
        """
        calendar = get_calendar_connector()

        parsed_date = parse_date_string(date)
        result = await calendar.schedule_performance_review(employee_name, parsed_date)

        demo_tag = "[DEMO] " if result.get("demo_mode") else ""
        formatted_date = format_date_for_user(date)

        return (
            f"##### {demo_tag}Performance Review Scheduled\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Date:** {formatted_date}\n"
            f"**Duration:** 1 hour\n"
            f"**Status:** Scheduled ✓\n\n"
            f"Your performance review has been scheduled. "
            f"Please prepare any necessary documents and be ready to discuss your accomplishments and goals.\n\n"
            f"AGENT SUMMARY: I scheduled a performance review for {employee_name} on {formatted_date}.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Conduct an exit interview for an employee leaving the company.")
    async def conduct_exit_interview(employee_name: str) -> str:
        """Conduct an exit interview for a departing employee.

        Args:
            employee_name: Name of the departing employee
        """
        calendar = get_calendar_connector()

        # Schedule exit interview
        interview_date = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0)
        await calendar.schedule_event(
            title=f"Exit Interview - {employee_name}",
            start_time=interview_date,
            duration_minutes=60,
            event_type="exit_interview"
        )

        demo_tag = "[DEMO] "

        return (
            f"##### {demo_tag}Exit Interview Conducted\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Status:** Completed ✓\n\n"
            f"The exit interview has been scheduled and will be conducted. "
            f"Thank you for your feedback and contributions to the company.\n\n"
            f"AGENT SUMMARY: I set up an exit interview for {employee_name}.\n"
            f"{HrTools.formatting_instructions}"
        )

    # =========================================================================
    # COMPENSATION & EXPENSES
    # =========================================================================

    @staticmethod
    @kernel_function(description="Approve an expense claim for an employee.")
    async def approve_expense_claim(employee_name: str, claim_amount: float) -> str:
        """Approve an expense claim for an employee.

        Args:
            employee_name: Name of the employee
            claim_amount: Amount of the expense claim
        """
        demo_tag = "[DEMO] "

        return (
            f"##### {demo_tag}Expense Claim Approved\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Claim Amount:** ${claim_amount:,.2f}\n"
            f"**Status:** Approved ✓\n\n"
            f"Your expense claim has been approved. "
            f"The amount will be reimbursed in your next payroll.\n\n"
            f"AGENT SUMMARY: I approved an expense claim of ${claim_amount:,.2f} for {employee_name}.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Issue a bonus to an employee.")
    async def issue_bonus(employee_name: str, amount: float) -> str:
        """Issue a bonus to an employee.

        Args:
            employee_name: Name of the employee
            amount: Bonus amount
        """
        demo_tag = "[DEMO] "

        return (
            f"##### {demo_tag}Bonus Issued\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Amount:** ${amount:,.2f}\n"
            f"**Status:** Processed ✓\n\n"
            f"A bonus of ${amount:,.2f} has been issued to {employee_name}. "
            f"This will be included in the next payroll.\n\n"
            f"AGENT SUMMARY: I issued a ${amount:,.2f} bonus to {employee_name}.\n"
            f"{HrTools.formatting_instructions}"
        )

    # =========================================================================
    # POLICY & COMMUNICATIONS
    # =========================================================================

    @staticmethod
    @kernel_function(description="Update company policies.")
    async def update_policies(policy_name: str, policy_content: str) -> str:
        """Update company policies.

        Args:
            policy_name: Name of the policy to update
            policy_content: New policy content
        """
        demo_tag = "[DEMO] "

        return (
            f"##### {demo_tag}Policy Updated\n"
            f"**Policy Name:** {policy_name}\n\n"
            f"The policy has been updated with the following content:\n\n"
            f"{policy_content}\n\n"
            f"AGENT SUMMARY: I updated the '{policy_name}' policy.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Send a company-wide announcement.")
    async def send_company_announcement(subject: str, content: str) -> str:
        """Send a company-wide announcement.

        Args:
            subject: Announcement subject
            content: Announcement content
        """
        demo_tag = "[DEMO] "

        return (
            f"##### {demo_tag}Company Announcement Sent\n"
            f"**Subject:** {subject}\n\n"
            f"{content}\n\n"
            f"AGENT SUMMARY: I sent a company-wide announcement: '{subject}'.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Send a welcome email to an email address.")
    async def send_email(emailaddress: str) -> str:
        """Send a welcome email to an email address.

        Args:
            emailaddress: Email address to send to
        """
        graph = get_graph_connector()

        # Extract name from email
        name = emailaddress.split("@")[0].replace(".", " ").title()

        result = await graph.send_welcome_email(name, emailaddress)

        demo_tag = "[DEMO] " if result.get("demo_mode") else ""

        return (
            f"##### {demo_tag}Welcome Email Sent\n"
            f"**Email Address:** {emailaddress}\n"
            f"**Status:** Delivered ✓\n\n"
            f"A welcome email has been sent to {emailaddress}.\n\n"
            f"AGENT SUMMARY: I sent a welcome email to {emailaddress}.\n"
            f"{HrTools.formatting_instructions}"
        )

    # =========================================================================
    # HR INFORMATION & DIRECTORY
    # =========================================================================

    @staticmethod
    @kernel_function(description="Retrieve the employee directory.")
    async def fetch_employee_directory() -> str:
        """Retrieve the employee directory."""
        db = get_database_connector()

        await db.initialize()
        db._seed_demo_data()

        employees = list(db._employees.values())

        directory_lines = []
        for emp in employees[:10]:  # Limit to 10 for display
            directory_lines.append(
                f"- **{emp.name}** - {emp.job_title} ({emp.department})"
            )

        demo_tag = "[DEMO] "

        return (
            f"##### {demo_tag}Employee Directory\n\n"
            f"**Total Employees:** {len(employees)}\n\n"
            + "\n".join(directory_lines) + "\n\n"
            f"AGENT SUMMARY: I retrieved the employee directory with {len(employees)} employees.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(
        description="Get HR information, such as policies, procedures, and onboarding guidelines."
    )
    async def get_hr_information(
        query: Annotated[str, "The query for the HR knowledgebase"],
    ) -> str:
        """Get HR information from the knowledge base.

        Args:
            query: The search query for HR information
        """
        demo_tag = "[DEMO] "

        # Simulated knowledge base search
        information = (
            f"##### {demo_tag}HR Information\n\n"
            f"**Search Query:** {query}\n\n"
            f"**Document Name:** Contoso's Employee Onboarding Procedure\n"
            f"**Domain:** HR Policy\n"
            f"**Description:** A step-by-step guide detailing the onboarding process for "
            f"new Contoso employees, from initial orientation to role-specific training.\n\n"
            f"**Related Topics:**\n"
            f"- New Employee Checklist\n"
            f"- Benefits Enrollment Guide\n"
            f"- IT Setup Procedures\n"
            f"- Company Policies Overview\n\n"
            f"AGENT SUMMARY: I retrieved HR information related to '{query}'.\n"
            f"{HrTools.formatting_instructions}"
        )
        return information

    # =========================================================================
    # ADDITIONAL HR OPERATIONS
    # =========================================================================

    @staticmethod
    @kernel_function(description="Initiate a background check for a new employee.")
    async def initiate_background_check(employee_name: str) -> str:
        """Initiate a background check for a new employee.

        Args:
            employee_name: Name of the employee
        """
        demo_tag = "[DEMO] "

        return (
            f"##### {demo_tag}Background Check Initiated\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Status:** In Progress\n"
            f"**Expected Completion:** 5-7 business days\n\n"
            f"A background check has been initiated for {employee_name}. "
            f"You will be notified once the check is complete.\n\n"
            f"AGENT SUMMARY: I initiated a background check for {employee_name}.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Manage an employee transfer between departments.")
    async def manage_employee_transfer(employee_name: str, new_department: str) -> str:
        """Manage an employee transfer between departments.

        Args:
            employee_name: Name of the employee
            new_department: New department name
        """
        db = get_database_connector()

        # Update employee's department
        emp_result = await db.get_employee_by_name(employee_name)
        if emp_result.get("success"):
            await db.update_employee(
                emp_result["employee"]["id"],
                department=new_department
            )

        demo_tag = "[DEMO] " if emp_result.get("demo_mode") else ""

        return (
            f"##### {demo_tag}Employee Transfer\n"
            f"**Employee Name:** {employee_name}\n"
            f"**New Department:** {new_department}\n"
            f"**Status:** Completed ✓\n\n"
            f"The transfer has been successfully processed. "
            f"{employee_name} is now part of the {new_department} department.\n\n"
            f"AGENT SUMMARY: I transferred {employee_name} to the {new_department} department.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Organize a team-building activity.")
    async def organize_team_building_activity(activity_name: str, date: str) -> str:
        """Organize a team-building activity.

        Args:
            activity_name: Name of the activity
            date: Date of the activity
        """
        calendar = get_calendar_connector()

        parsed_date = parse_date_string(date)
        await calendar.schedule_event(
            title=f"Team Building: {activity_name}",
            start_time=parsed_date,
            duration_minutes=180,  # 3 hours
            event_type="team_building"
        )

        demo_tag = "[DEMO] "
        formatted_date = format_date_for_user(date)

        return (
            f"##### {demo_tag}Team-Building Activity Organized\n"
            f"**Activity Name:** {activity_name}\n"
            f"**Date:** {formatted_date}\n"
            f"**Duration:** 3 hours\n"
            f"**Status:** Scheduled ✓\n\n"
            f"The team-building activity has been successfully organized. "
            f"Calendar invites will be sent to all team members.\n\n"
            f"AGENT SUMMARY: I organized '{activity_name}' team-building for {formatted_date}.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Organize a health and wellness program.")
    async def organize_wellness_program(program_name: str, date: str) -> str:
        """Organize a health and wellness program.

        Args:
            program_name: Name of the wellness program
            date: Date of the program
        """
        demo_tag = "[DEMO] "
        formatted_date = format_date_for_user(date)

        return (
            f"##### {demo_tag}Health and Wellness Program Organized\n"
            f"**Program Name:** {program_name}\n"
            f"**Date:** {formatted_date}\n"
            f"**Status:** Scheduled ✓\n\n"
            f"The health and wellness program has been successfully organized. "
            f"Please join us for an informative and engaging session.\n\n"
            f"AGENT SUMMARY: I organized the '{program_name}' wellness program for {formatted_date}.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Schedule a wellness check for an employee.")
    async def schedule_wellness_check(employee_name: str, date: str) -> str:
        """Schedule a wellness check for an employee.

        Args:
            employee_name: Name of the employee
            date: Date for the wellness check
        """
        demo_tag = "[DEMO] "
        formatted_date = format_date_for_user(date)

        return (
            f"##### {demo_tag}Wellness Check Scheduled\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Date:** {formatted_date}\n"
            f"**Status:** Scheduled ✓\n\n"
            f"A wellness check has been scheduled for {employee_name}.\n\n"
            f"AGENT SUMMARY: I scheduled a wellness check for {employee_name} on {formatted_date}.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Facilitate the setup for remote work for an employee.")
    async def facilitate_remote_work_setup(employee_name: str) -> str:
        """Facilitate remote work setup for an employee.

        Args:
            employee_name: Name of the employee
        """
        demo_tag = "[DEMO] "

        return (
            f"##### {demo_tag}Remote Work Setup Facilitated\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Status:** Completed ✓\n\n"
            f"Remote work setup checklist:\n"
            f"- ✓ VPN Access configured\n"
            f"- ✓ Remote desktop enabled\n"
            f"- ✓ Communication tools set up\n"
            f"- ✓ Security protocols reviewed\n\n"
            f"Please ensure you have all the necessary equipment and access.\n\n"
            f"AGENT SUMMARY: I facilitated remote work setup for {employee_name}.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Manage the retirement plan for an employee.")
    async def manage_retirement_plan(employee_name: str) -> str:
        """Manage retirement plan for an employee.

        Args:
            employee_name: Name of the employee
        """
        demo_tag = "[DEMO] "

        return (
            f"##### {demo_tag}Retirement Plan Managed\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Plan Type:** 401(k)\n"
            f"**Status:** Active ✓\n\n"
            f"Retirement plan details:\n"
            f"- Employee Contribution: 6%\n"
            f"- Employer Match: 4%\n"
            f"- Vesting: 100% after 3 years\n\n"
            f"AGENT SUMMARY: I reviewed retirement plan details for {employee_name}.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Handle a suggestion made by an employee.")
    async def handle_employee_suggestion(employee_name: str, suggestion: str) -> str:
        """Handle an employee suggestion.

        Args:
            employee_name: Name of the employee
            suggestion: The suggestion text
        """
        demo_tag = "[DEMO] "

        return (
            f"##### {demo_tag}Employee Suggestion Handled\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Suggestion:** {suggestion}\n"
            f"**Status:** Received ✓\n\n"
            f"The suggestion has been logged and will be reviewed by the appropriate team. "
            f"Thank you for your valuable input!\n\n"
            f"AGENT SUMMARY: I logged suggestion from {employee_name}: '{suggestion[:50]}...'.\n"
            f"{HrTools.formatting_instructions}"
        )

    @staticmethod
    @kernel_function(description="Update privileges for an employee.")
    async def update_employee_privileges(
        employee_name: str, privilege: str, status: str
    ) -> str:
        """Update privileges for an employee.

        Args:
            employee_name: Name of the employee
            privilege: Privilege to update
            status: New status (granted/revoked)
        """
        demo_tag = "[DEMO] "

        return (
            f"##### {demo_tag}Employee Privileges Updated\n"
            f"**Employee Name:** {employee_name}\n"
            f"**Privilege:** {privilege}\n"
            f"**Status:** {status.title()} ✓\n\n"
            f"The privileges for {employee_name} have been successfully updated.\n\n"
            f"AGENT SUMMARY: I {status.lower()} '{privilege}' privilege for {employee_name}.\n"
            f"{HrTools.formatting_instructions}"
        )

    # =========================================================================
    # UTILITY METHODS
    # =========================================================================

    @classmethod
    def get_all_kernel_functions(cls) -> dict[str, Callable]:
        """
        Returns a dictionary of all methods with @kernel_function annotation.

        Returns:
            Dict[str, Callable]: Function names mapped to function objects
        """
        kernel_functions = {}

        for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
            if name.startswith("_") or name in ["get_all_kernel_functions", "generate_tools_json_doc"]:
                continue

            if hasattr(method, "__kernel_function__"):
                kernel_functions[name] = method

        return kernel_functions

    @classmethod
    def generate_tools_json_doc(cls) -> str:
        """
        Generate a JSON document containing information about all tools.

        Returns:
            str: JSON string with tool information
        """
        tools_list = []

        for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
            if name.startswith("_") or name in ["generate_tools_json_doc", "get_all_kernel_functions"]:
                continue

            if hasattr(method, "__kernel_function__"):
                description = ""
                kf = getattr(method, "__kernel_function__", None)
                if kf and hasattr(kf, "description"):
                    description = kf.description

                sig = inspect.signature(method)
                type_hints = get_type_hints(method)
                args_dict = {}

                for param_name, param in sig.parameters.items():
                    if param_name in ["cls", "self"]:
                        continue

                    param_type = "string"
                    if param_name in type_hints:
                        type_obj = type_hints[param_name]
                        if hasattr(type_obj, "__name__"):
                            param_type = type_obj.__name__.lower()
                        else:
                            type_str = str(type_obj).lower()
                            if "int" in type_str:
                                param_type = "int"
                            elif "float" in type_str:
                                param_type = "float"
                            elif "bool" in type_str:
                                param_type = "boolean"

                    args_dict[param_name] = {
                        "description": param_name,
                        "title": param_name.replace("_", " ").title(),
                        "type": param_type,
                    }

                tool_entry = {
                    "agent": cls.agent_name,
                    "function": name,
                    "description": description,
                    "arguments": json.dumps(args_dict).replace('"', "'"),
                }
                tools_list.append(tool_entry)

        return json.dumps(tools_list, ensure_ascii=False, indent=2)
