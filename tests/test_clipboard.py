"""Tests for the shared clipboard helper (wl-copy/xclip/xsel fallback)."""

from meeting_notes import clipboard


class FakeProc:
    def __init__(self, record):
        self.record = record

    def communicate(self, data):
        self.record["data"] = data
        return (b"", b"")


def _patch(monkeypatch, available, record):
    monkeypatch.setattr(
        "shutil.which", lambda tool: f"/usr/bin/{tool}" if tool in available else None
    )
    def fake_popen(cmd, stdin=None):
        record["cmd"] = cmd
        return FakeProc(record)
    monkeypatch.setattr("subprocess.Popen", fake_popen)


def test_prefers_wl_copy_on_wayland(monkeypatch):
    record = {}
    _patch(monkeypatch, {"wl-copy", "xclip", "xsel"}, record)
    ok, msg = clipboard.copy_text_to_clipboard("hello")
    assert ok
    assert record["cmd"] == ["wl-copy"]
    assert record["data"] == b"hello"


def test_falls_back_to_xclip(monkeypatch):
    record = {}
    _patch(monkeypatch, {"xclip"}, record)
    ok, _ = clipboard.copy_text_to_clipboard("hi")
    assert ok
    assert record["cmd"] == ["xclip", "-selection", "clipboard"]


def test_no_tool_reports_helpful_error(monkeypatch):
    record = {}
    _patch(monkeypatch, set(), record)
    ok, msg = clipboard.copy_text_to_clipboard("hi")
    assert not ok
    assert "wl-clipboard" in msg and "cmd" not in record
