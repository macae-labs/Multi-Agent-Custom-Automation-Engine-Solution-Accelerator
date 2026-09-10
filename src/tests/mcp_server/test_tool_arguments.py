"""Deterministic argument conformance for ``call_external_tool``.

Reproduces the Higgsfield incident: the model called ``job_status`` with
``job_id`` while the server declares ``jobId``; the remote rejected the call
and ca-mcp reported that rejection as ``TOOL SUCCESS``. The proxy now conforms
the arguments to the declared ``inputSchema`` BEFORE the network call, rejects
locally what the server would reject anyway, and honors ``isError``.
"""

import asyncio
import json

import pytest

fastmcp = pytest.importorskip("fastmcp")

from utils.tool_arguments import (  # noqa: E402
    canonical_key,
    normalize_arguments,
    validate_arguments,
)

JOB_ID = "512fda58-82c1-44db-8a66-49ce9263fa94"
MP4 = f"https://d8j0ntlcm91z4.cloudfront.net/u/hf_{JOB_ID}.mp4"

JOB_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "jobId": {"type": "string"},
        "sync": {"type": "boolean"},
    },
    "required": ["jobId"],
    "additionalProperties": False,
}

GENERATE_VIDEO_SCHEMA = {
    "type": "object",
    "properties": {
        "params": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "prompt": {"type": "string"},
                "aspectRatio": {"type": "string"},
            },
            "required": ["model", "prompt"],
        }
    },
    "required": ["params"],
}


class TestCanonicalKey:
    def test_collapses_case_and_separators(self):
        assert canonical_key("job_id") == canonical_key("jobId")
        assert canonical_key("JOB-ID") == "jobid"


class TestNormalizeArguments:
    def test_renames_unambiguous_variant_to_declared_name(self):
        out = normalize_arguments({"job_id": JOB_ID, "sync": True}, JOB_STATUS_SCHEMA)
        assert out.arguments == {"jobId": JOB_ID, "sync": True}
        assert out.renamed == {"job_id": "jobId"}

    def test_declared_names_are_untouched(self):
        out = normalize_arguments({"jobId": JOB_ID}, JOB_STATUS_SCHEMA)
        assert out.arguments == {"jobId": JOB_ID}
        assert out.renamed == {}

    def test_never_overwrites_an_explicitly_supplied_property(self):
        args = {"jobId": "declared", "job_id": "variant"}
        out = normalize_arguments(args, JOB_STATUS_SCHEMA)
        assert out.arguments == args
        assert out.renamed == {}

    def test_ambiguous_canonical_match_is_left_alone(self):
        schema = {
            "type": "object",
            "properties": {"userId": {}, "user_id": {}},
        }
        out = normalize_arguments({"USERID": 1}, schema)
        assert out.arguments == {"USERID": 1}
        assert out.renamed == {}

    def test_recurses_into_nested_object_properties(self):
        args = {"params": {"model": "seedance_2_0", "prompt": "p", "aspect_ratio": "16:9"}}
        out = normalize_arguments(args, GENERATE_VIDEO_SCHEMA)
        assert out.arguments["params"] == {
            "model": "seedance_2_0",
            "prompt": "p",
            "aspectRatio": "16:9",
        }
        assert out.renamed == {"params.aspect_ratio": "params.aspectRatio"}

    def test_without_properties_is_a_passthrough(self):
        out = normalize_arguments({"job_id": JOB_ID}, {"type": "object"})
        assert out.arguments == {"job_id": JOB_ID}
        assert out.renamed == {}
        assert normalize_arguments({"a": 1}, None).arguments == {"a": 1}


class TestValidateArguments:
    def test_missing_required_is_reported(self):
        check = validate_arguments({"sync": True}, JOB_STATUS_SCHEMA)
        assert check.missing_required == ["jobId"]
        assert not check.ok

    def test_unknown_key_reported_only_when_additional_properties_false(self):
        strict = validate_arguments({"jobId": JOB_ID, "verbose": 1}, JOB_STATUS_SCHEMA)
        assert strict.unknown == ["verbose"]
        lenient = validate_arguments(
            {"params": {"model": "m", "prompt": "p"}, "extra": 1}, GENERATE_VIDEO_SCHEMA
        )
        assert lenient.ok

    def test_nested_required_uses_dotted_paths(self):
        check = validate_arguments({"params": {"model": "m"}}, GENERATE_VIDEO_SCHEMA)
        assert check.missing_required == ["params.prompt"]

    def test_conforming_call_is_ok(self):
        assert validate_arguments({"jobId": JOB_ID, "sync": True}, JOB_STATUS_SCHEMA).ok


# --- call_external_tool end-to-end with a fake external session --------------


def _higgsfield_like(name, arguments):
    """Behaves like Higgsfield's MCP server for job_status: validation
    failures come back as a tool result with isError=true, not as a
    JSON-RPC error."""
    if name == "job_status" and "jobId" not in arguments:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Input validation error: Invalid arguments for tool "
                        "job_status: jobId: Invalid input: expected string, "
                        "received undefined"
                    ),
                }
            ],
            "isError": True,
        }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"id": arguments.get("jobId"), "status": "completed", "url": MP4}),
            }
        ]
    }


@pytest.fixture
def external_tool(mock_mcp_server):
    """(call_external_tool, install_session) bound to a fresh InspectorService."""
    from services.inspector_service import InspectorService, ToolSchemaCache

    class FakeSession(ToolSchemaCache):
        def __init__(self, tools, responder):
            self._tools = tools
            self._responder = responder
            self.calls = []
            self.server_url = "https://fake.example/mcp"
            self.server_info = {"name": "fake"}

        async def list_tools(self):
            return self._tools

        async def call_tool(self, tool_name, arguments):
            self.calls.append((tool_name, dict(arguments)))
            return self._responder(tool_name, arguments)

    svc = InspectorService()
    svc.register_tools(mock_mcp_server)
    fn = next(
        t["func"] for t in mock_mcp_server.tools if t["func"].__name__ == "call_external_tool"
    )

    def install(tools, responder=_higgsfield_like, user="u1", server="higgsfield"):
        sess = FakeSession(tools, responder)
        svc._sessions[(user, server)] = sess
        return sess

    return fn, install


LISTED = [{"name": "job_status", "description": "", "inputSchema": JOB_STATUS_SCHEMA}]


# Success responses are JSON (format_success_response); error responses are
# the Markdown block of format_error_response ("##### ❌ Error" + **Error:**).
def _run(coro) -> str:
    return asyncio.run(coro)


class TestCallExternalTool:
    def test_snake_case_argument_is_conformed_and_the_call_succeeds(self, external_tool):
        fn, install = external_tool
        sess = install(LISTED)
        res = json.loads(
            _run(
                fn(
                    server_name="higgsfield",
                    target_tool="job_status",
                    arguments={"job_id": JOB_ID, "sync": True},
                    user_id="u1",
                )
            )
        )
        assert sess.calls == [("job_status", {"jobId": JOB_ID, "sync": True})]
        assert res["status"] == "success"
        assert res["details"]["renamed_arguments"] == {"job_id": "jobId"}
        assert MP4 in res["details"]["result"]

    def test_missing_required_is_rejected_before_any_network_call(self, external_tool):
        fn, install = external_tool
        sess = install(LISTED)
        raw = _run(
            fn(
                server_name="higgsfield",
                target_tool="job_status",
                arguments={"sync": True},
                user_id="u1",
            )
        )
        assert sess.calls == []
        assert "❌ Error" in raw
        assert "missing required ['jobId']" in raw
        assert "NOT sent" in raw
        assert '"jobId"' in raw  # schema included for a one-step retry
        assert "TOOL SUCCESS" not in raw

    def test_unlisted_tool_passes_through_untouched(self, external_tool):
        fn, install = external_tool
        sess = install([])  # server lists nothing (hidden members / pagination)
        _run(
            fn(
                server_name="higgsfield",
                target_tool="secret_member",
                arguments={"job_id": JOB_ID},
                user_id="u1",
            )
        )
        assert sess.calls == [("secret_member", {"job_id": JOB_ID})]

    def test_remote_is_error_is_reported_as_an_error_not_success(self, external_tool):
        fn, install = external_tool
        # Schema unknown → no conformance → the remote rejects with isError.
        sess = install([])
        raw = _run(
            fn(
                server_name="higgsfield",
                target_tool="job_status",
                arguments={"job_id": JOB_ID},
                user_id="u1",
            )
        )
        assert sess.calls == [("job_status", {"job_id": JOB_ID})]
        assert "❌ Error" in raw
        assert "Input validation error" in raw
        assert "TOOL SUCCESS" not in raw

    def test_schema_index_is_cached_across_calls(self, external_tool):
        fn, install = external_tool
        sess = install(LISTED)
        listed = 0
        original = sess.list_tools

        async def counting():
            nonlocal listed
            listed += 1
            return await original()

        sess.list_tools = counting
        for _ in range(3):
            _run(
                fn(
                    server_name="higgsfield",
                    target_tool="job_status",
                    arguments={"jobId": JOB_ID},
                    user_id="u1",
                )
            )
        assert listed == 1
