"""La narración de tools (carril 2 de voz y la UI) debe nombrar la tool REAL.

En la vía hospedada toda llamada externa pasa por el envoltorio
``call_external_tool`` de MacaeMcpServer, y en el Toolbox por ``call_tool``
un nivel más adentro. La voz decía "Consultando call external tool en
MacaeMcpServer" cuando la llamada real era GitHub___list_commits en tool-box.
"""

import json

from backend.v4.api.router import _describe_tool_call


def test_plain_tool_is_unchanged():
    assert _describe_tool_call(
        "workspace_exec", "MacaeMcpServer", '{"command":"ls"}'
    ) == (
        "workspace_exec",
        "MacaeMcpServer",
    )


def test_call_external_tool_exposes_target_tool_and_server():
    args = json.dumps(
        {
            "server_name": "higgsfield",
            "target_tool": "job_status",
            "arguments": {"jobId": "x"},
        }
    )
    assert _describe_tool_call("call_external_tool", "MacaeMcpServer", args) == (
        "job_status",
        "higgsfield",
    )


def test_toolbox_call_tool_exposes_the_nested_member_name():
    args = {
        "server_name": "tool-box",
        "target_tool": "call_tool",
        "arguments": {
            "name": "GitHub___list_commits",
            "arguments": {"owner": "macae-labs"},
        },
    }
    assert _describe_tool_call("call_external_tool", "MacaeMcpServer", args) == (
        "GitHub___list_commits",
        "tool-box",
    )


def test_malformed_arguments_fall_back_to_the_wrapper_names():
    assert _describe_tool_call("call_external_tool", "MacaeMcpServer", "{not json") == (
        "call_external_tool",
        "MacaeMcpServer",
    )
    assert _describe_tool_call("call_external_tool", "MacaeMcpServer", None) == (
        "call_external_tool",
        "MacaeMcpServer",
    )
