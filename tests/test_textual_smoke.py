"""Headless Textual smoke tests using App.run_test().

These are intentionally minimal — full UI flows are slow and brittle.
We just want to catch:
  - The app actually starts without raising
  - The settings screen opens
  - Switching providers in settings doesn't crash with the duplicate-ID
    error (the bug PR #9 fixed; this is a regression guard)

These tests transitively import faster_whisper (via meeting_notes.app →
meeting_notes.transcriber). CI deliberately skips
this file to keep install time fast — see .github/workflows/ci.yml. To
run locally:

    pip install -e ".[all,dev]"
    pytest tests/test_textual_smoke.py
"""
import pytest

# Skip the entire module if the heavy deps (whisper / textual) aren't
# installed. Avoids confusing import errors for contributors who only
# installed the lightweight test deps.
pytest.importorskip("faster_whisper", reason="run `pip install -e .[all,dev]` to enable Textual smoke tests")
pytest.importorskip("textual", reason="run `pip install -e .[all,dev]` to enable Textual smoke tests")

from meeting_notes.app import MeetingNotesApp  # noqa: E402  (deliberate import-after-skip)


@pytest.mark.asyncio
async def test_app_starts_and_exits_cleanly(tmp_path, monkeypatch):
    """The app should mount cleanly in headless mode and respond to ctrl+c-equivalent."""
    # Sandbox config & data dirs so the test doesn't touch real ones
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    app = MeetingNotesApp()
    async with app.run_test() as pilot:
        # Just let the app stabilise. If anything raises during mount,
        # we'd see it here.
        await pilot.pause()
        assert app.is_running
        # Quit cleanly
        app.exit()


@pytest.mark.asyncio
async def test_settings_screen_opens(tmp_path, monkeypatch):
    """Pressing ',' should open the settings screen without error."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    app = MeetingNotesApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press(",")
        await pilot.pause()
        # SettingsScreen should now be on the screen stack.
        # (We don't import it for an isinstance check — its exact import
        # path isn't load-bearing; just confirm the stack changed.)
        assert len(app.screen_stack) >= 2, "settings screen should have been pushed"
        app.exit()


@pytest.mark.asyncio
async def test_switching_providers_does_not_duplicate_widget_ids(tmp_path, monkeypatch):
    """Regression test for issue #11 / PR #9.

    Switching AI providers used to crash with `DuplicateIds: provider-openai`
    because remove_children() wasn't awaited before mount(). This test
    rapidly clicks between providers and asserts no exception.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    app = MeetingNotesApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press(",")  # open settings
        await pilot.pause()

        # Click each provider button in turn.  If remove_children isn't
        # awaited, the second mount of any provider button will raise
        # DuplicateIds.
        provider_ids = ["provider-openai", "provider-anthropic",
                        "provider-openrouter", "provider-anthropic"]
        for pid in provider_ids:
            try:
                await pilot.click(f"#{pid}")
                await pilot.pause()
            except Exception as e:
                # Surface DuplicateIds clearly if it ever comes back
                if "Duplicate" in type(e).__name__ or "already exists" in str(e):
                    pytest.fail(f"PR #9 regressed — DuplicateIds when clicking {pid}: {e}")
                # Other failures (e.g. button not found because layout
                # changed) shouldn't fail this specific regression test
                # — re-raise to fail loudly so the test gets updated.
                raise

        app.exit()


@pytest.mark.asyncio
async def test_new_settings_sections_render(tmp_path, monkeypatch):
    """Transcription / Obsidian / Voice Tags sections mount without error
    and re-render cleanly (no duplicate widget IDs on revisit)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    app = MeetingNotesApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press(",")
        await pilot.pause()

        for section in ["section-obsidian", "section-voices", "section-ai"]:
            await pilot.click(f"#{section}")
            await pilot.pause()

        # Transcription lives inside AI Models (merged sections) — the
        # provider toggle and whisper model picker render there.
        assert app.screen.query("#transprov-local")
        assert app.screen.query("#transprov-openai")
        assert app.screen.query("#whispermodel-base")
        # No separate Transcription sidebar entry remains.
        assert not app.screen.query("#section-transcription")
        app.exit()


@pytest.mark.asyncio
async def test_transcription_provider_toggle_updates_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    app = MeetingNotesApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press(",")
        await pilot.pause()
        # AI Models is the default section; the provider toggle is in it
        # (below the summarization widgets — scroll it into view first).
        app.screen.query_one("#transprov-openai").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#transprov-openai")
        await pilot.pause()
        assert app.screen.config["transcription_provider"] == "openai"
        app.screen.query_one("#transprov-local").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#transprov-local")
        await pilot.pause()
        assert app.screen.config["transcription_provider"] == "local"
        app.exit()


@pytest.mark.asyncio
async def test_sigusr1_starts_recording_when_idle(tmp_path, monkeypatch):
    """Ctrl+Alt+M's launcher sends SIGUSR1 to a running app to start
    recording. A real signal must reach _on_remote_record and trigger
    action_start_recording (stubbed — no real audio processes)."""
    import asyncio
    import os
    import signal

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    app = MeetingNotesApp()
    started = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_start_recording = lambda: started.append(True)
        os.kill(os.getpid(), signal.SIGUSR1)
        # add_signal_handler delivers via the event loop; give it a beat.
        await asyncio.sleep(0.2)
        await pilot.pause()
        assert started, "SIGUSR1 should have triggered action_start_recording"
        app.exit()


@pytest.mark.asyncio
async def test_sigusr1_is_noop_while_recording(tmp_path, monkeypatch):
    """If a recording is already running, the remote request must not
    start another one — just a notification."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    app = MeetingNotesApp()
    started = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_start_recording = lambda: started.append(True)
        app.is_recording = True
        app._on_remote_record()
        await pilot.pause()
        assert not started, "must not start a second recording"
        app.exit()


@pytest.mark.asyncio
async def test_sigusr2_and_rtmin1_drive_stop_and_cancel(tmp_path, monkeypatch):
    """Ctrl+Alt+S / Ctrl+Alt+X send SIGUSR2 / SIGRTMIN+1: while recording
    they must dispatch to stop/cancel; while idle they must be no-ops."""
    import asyncio
    import os
    import signal

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    app = MeetingNotesApp()
    calls = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_stop_recording = lambda: calls.append("stop")
        app.action_cancel_recording = lambda: calls.append("cancel")

        # Idle: both signals are guarded no-ops
        app._on_remote_stop()
        app._on_remote_cancel()
        assert calls == []

        # Recording: real signals must reach the right actions
        app.is_recording = True
        os.kill(os.getpid(), signal.SIGUSR2)
        await asyncio.sleep(0.2)
        await pilot.pause()
        assert calls == ["stop"]

        os.kill(os.getpid(), signal.SIGRTMIN + 1)
        await asyncio.sleep(0.2)
        await pilot.pause()
        assert calls == ["stop", "cancel"]
        app.exit()
