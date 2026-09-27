"""
Tests for workspace MCP service behaviors.
"""

from json import JSONDecodeError, loads
import subprocess

import pytest

fastmcp = pytest.importorskip("fastmcp")

from core.factory import Domain  # noqa: E402
from services import workspace_service  # noqa: E402


@pytest.fixture
def workspace_tools(mock_mcp_server):
    """Register workspace tools and return them by function name."""
    service = workspace_service.WorkspaceToolService()
    service.register_tools(mock_mcp_server)

    return {
        tool["func"].__name__: tool["func"] for tool in mock_mcp_server.tools
    }, mock_mcp_server


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    """Point workspace resolution at a temporary root."""
    root = (tmp_path / "workspaces").resolve()
    root.mkdir()
    monkeypatch.setattr(workspace_service, "WORKSPACE_ROOT", root)
    return root


def _make_workspace(root, user_id="user-1", workspace_id="workspace-1"):
    workspace = root / user_id / workspace_id
    workspace.mkdir(parents=True)
    return workspace, user_id, workspace_id


def _init_git_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)


class TestWorkspaceToolService:
    """Test cases for workspace tools."""

    def test_register_tools(self, workspace_tools):
        """Test tool registration."""
        tools, mock_mcp_server = workspace_tools
        service = workspace_service.WorkspaceToolService()

        assert len(mock_mcp_server.tools) == service.tool_count
        assert "workspace_git_status" in tools
        assert "workspace_write_file" in tools
        for tool in mock_mcp_server.tools:
            assert Domain.WORKSPACE.value in tool["tags"]

    def test_workspace_git_status_returns_error_on_git_failure(
        self, workspace_root, workspace_tools, monkeypatch
    ):
        """Test git status surfaces git command failures."""
        tools, _ = workspace_tools
        _, user_id, workspace_id = _make_workspace(workspace_root)

        def mock_git(_ws, *args):
            assert args == ("status", "--short")
            return subprocess.CompletedProcess(
                ["git", *args], 128, stdout=b"", stderr=b"fatal: not a git repository"
            )

        monkeypatch.setattr(workspace_service, "_git", mock_git)

        result = tools["workspace_git_status"](user_id, workspace_id)

        assert "##### ❌ Error" in result
        assert "**Context:** workspace_git_status" in result
        assert "git status failed: fatal: not a git repository" in result

    def test_workspace_write_file_rejects_non_git_workspace(
        self, workspace_root, workspace_tools
    ):
        """Test writes are rejected before mutating a non-git workspace."""
        tools, _ = workspace_tools
        workspace, user_id, workspace_id = _make_workspace(workspace_root)

        result = tools["workspace_write_file"](
            user_id, workspace_id, "notes.txt", "hello"
        )

        assert "Workspace is not a git repository" in result
        assert not (workspace / "notes.txt").exists()

    def test_workspace_write_file_rejects_path_outside_workspace(
        self, workspace_root, workspace_tools
    ):
        """Test writes cannot escape the workspace root."""
        tools, _ = workspace_tools
        workspace, user_id, workspace_id = _make_workspace(workspace_root)
        _init_git_repo(workspace)

        result = tools["workspace_write_file"](
            user_id, workspace_id, "../escape.txt", "hello"
        )

        assert "Path outside workspace." in result
        assert not (workspace.parent / "escape.txt").exists()

    def test_workspace_write_file_rejects_content_over_size_limit(
        self, workspace_root, workspace_tools
    ):
        """Test writes over the max file size are rejected."""
        tools, _ = workspace_tools
        workspace, user_id, workspace_id = _make_workspace(workspace_root)
        _init_git_repo(workspace)
        content = "a" * (workspace_service.MAX_FILE_BYTES + 1)

        result = tools["workspace_write_file"](
            user_id, workspace_id, "large.txt", content
        )

        assert (
            f"File too large ({len(content)} bytes). Max is "
            f"{workspace_service.MAX_FILE_BYTES} bytes."
        ) in result
        assert not (workspace / "large.txt").exists()

    def test_workspace_write_file_writes_and_commits(
        self, workspace_root, workspace_tools
    ):
        """Test successful writes are committed immediately."""
        tools, _ = workspace_tools
        workspace, user_id, workspace_id = _make_workspace(workspace_root)
        _init_git_repo(workspace)

        result = tools["workspace_write_file"](
            user_id, workspace_id, "notes.txt", "hello", "agent: write notes.txt"
        )

        try:
            payload = loads(result)
        except JSONDecodeError as exc:
            pytest.fail(f"Expected a JSON success response, got: {result} ({exc})")
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        commit_message = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )

        assert payload["status"] == "success"
        assert payload["action"] == "workspace_write_file"
        assert payload["details"] == {"path": "notes.txt", "bytes": 5}
        assert payload["summary"] == "Wrote 5 bytes to 'notes.txt' and committed."
        assert (workspace / "notes.txt").read_text(encoding="utf-8") == "hello"
        assert status.stdout.strip() == ""
        assert commit_message.stdout.strip() == "agent: write notes.txt"


# ── dueño ajeno en el share ──────────────────────────────────────────────────
# Sobre Azure Files/SMB el árbol no pertenece al uid del proceso y git rechaza
# TODA operación con "detected dubious ownership" (medido en prod:
# workspace_git_status del clon de /data/workspaces). La condición se reproduce
# de verdad con GIT_TEST_ASSUME_DIFFERENT_OWNER, no con un mock del fallo.


class TestDubiousOwnership:
    def test_the_condition_is_real_without_the_declared_trust(
        self, workspace_root, monkeypatch
    ):
        workspace, _, _ = _make_workspace(workspace_root)
        _init_git_repo(workspace)
        monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

        bare = subprocess.run(
            ["git", "status", "--short"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )

        assert bare.returncode != 0
        assert "dubious ownership" in bare.stderr

    def test_git_tools_work_on_a_tree_owned_by_someone_else(
        self, workspace_tools, workspace_root, monkeypatch
    ):
        tools, _ = workspace_tools
        workspace, user_id, workspace_id = _make_workspace(workspace_root)
        _init_git_repo(workspace)
        monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

        result = tools["workspace_git_status"](user_id, workspace_id)

        assert "dubious ownership" not in result
        assert "❌" not in result

    def test_exec_carries_the_trust_so_the_agent_can_run_git_itself(
        self, workspace_root
    ):
        workspace, _, _ = _make_workspace(workspace_root)

        env = workspace_service._child_env(workspace)

        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
        assert env["GIT_CONFIG_VALUE_0"] == str(workspace)
        # Sin árbol declarado el entorno queda como estaba.
        assert "GIT_CONFIG_COUNT" not in workspace_service._child_env()
