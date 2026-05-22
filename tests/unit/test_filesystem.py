"""Tests for filesystem tools path resolution and workspace fence."""

from pathlib import Path

import pytest

from agent.tools.filesystem import ReadFileTool, WriteFileTool, _FsTool


class TestResolvePath:
    """Verify path resolution respects workspace boundaries."""

    def test_relative_path_within_workspace(self, tmp_path: Path) -> None:
        tool = ReadFileTool(workspace=tmp_path)
        result = tool._resolve("foo/bar.txt")
        assert result == (tmp_path / "foo" / "bar.txt").resolve()

    def test_absolute_path_allowed_when_no_workspace(self) -> None:
        tool = ReadFileTool(workspace=None)
        result = tool._resolve("/tmp/test.txt")
        assert result == Path("/tmp/test.txt").resolve()

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        tool = ReadFileTool(workspace=tmp_path)
        with pytest.raises(ValueError, match="Path outside workspace"):
            tool._resolve("../escaped.txt")

    def test_deep_traversal_blocked(self, tmp_path: Path) -> None:
        tool = ReadFileTool(workspace=tmp_path)
        with pytest.raises(ValueError, match="Path outside workspace"):
            tool._resolve("foo/../../../etc/passwd")

    def test_absolute_path_blocked_with_workspace(self, tmp_path: Path) -> None:
        tool = ReadFileTool(workspace=tmp_path)
        with pytest.raises(ValueError, match="Path outside workspace"):
            tool._resolve("/etc/passwd")

    def test_expanduser_blocked(self, tmp_path: Path) -> None:
        tool = ReadFileTool(workspace=tmp_path)
        with pytest.raises(ValueError, match="Path outside workspace"):
            tool._resolve("~/.ssh/id_rsa")


class TestReadFileTool:
    """Verify read_file tool operations."""

    def test_read_file(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world\nline 2\n")
        tool = ReadFileTool(workspace=tmp_path)
        result = tool.execute(path="test.txt")
        assert "hello world" in result

    def test_read_nonexistent_file(self, tmp_path: Path) -> None:
        tool = ReadFileTool(workspace=tmp_path)
        result = tool.execute(path="does_not_exist.txt")
        assert result.startswith("Error")

    def test_read_outside_workspace(self, tmp_path: Path) -> None:
        tool = ReadFileTool(workspace=tmp_path)
        result = tool.execute(path="/etc/passwd")
        assert result.startswith("Error")


class TestWriteFileTool:
    """Verify write_file tool operations."""

    def test_write_file(self, tmp_path: Path) -> None:
        tool = WriteFileTool(workspace=tmp_path)
        result = tool.execute(path="new_file.txt", content="hello world")
        assert (tmp_path / "new_file.txt").read_text() == "hello world"

    def test_write_outside_workspace(self, tmp_path: Path) -> None:
        tool = WriteFileTool(workspace=tmp_path)
        result = tool.execute(path="/etc/passwd", content="evil")
        assert result.startswith("Error")
