"""
Tests for RecORDER.py, driven through a hand-written obspython stub.

Run with:  python -m unittest discover -s tests -v
"""

import importlib.util
import inspect
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)

if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

import obspython as obs  # noqa: E402  (must come from tests/, before RecORDER is imported)

sys.modules["obspython"] = obs


def _load_recorder():
    path = os.path.join(PROJECT_DIR, "RecORDER.py")
    spec = importlib.util.spec_from_file_location("recorder_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


recorder = _load_recorder()


TUNABLES = (
    "MOVE_MAX_ATTEMPTS",
    "MOVE_RETRY_BASE_SECONDS",
    "MOVE_RETRY_MAX_SECONDS",
    "SIZE_STABLE_POLL_SECONDS",
    "SIZE_STABLE_MAX_SAMPLES",
    "REMUX_POLL_SECONDS",
    "REMUX_TIMEOUT_SECONDS",
    "WORKER_JOIN_TIMEOUT_SECONDS",
)


class TimeShim:
    """Lets a test jump forward in time without touching the real time module."""

    def __init__(self):
        self.offset = 0.0

    def monotonic(self):
        return time.monotonic() + self.offset

    def time(self):
        return time.time()

    def sleep(self, seconds):
        time.sleep(seconds)

    def advance(self, seconds):
        self.offset += seconds


class FlakyShutil:
    """shutil stand-in that fails the first N moves with a sharing violation."""

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def move(self, source, destination):
        self.calls += 1
        if self.calls <= self.failures:
            raise PermissionError(32, "The process cannot access the file because it is "
                                      "being used by another process")
        return shutil.move(source, destination)


class BlockingShutil:
    """
    Like FlakyShutil, but the failing attempts park inside move() until the test
    releases them - so a test can be sure a job is genuinely in flight.
    """

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0
        self.first_call = threading.Event()
        self.release = threading.Event()

    def move(self, source, destination):
        self.calls += 1
        self.first_call.set()
        if self.calls <= self.failures:
            self.release.wait(5.0)
            raise PermissionError(32, "The process cannot access the file because it is "
                                      "being used by another process")
        return shutil.move(source, destination)


class RecORDERTestCase(unittest.TestCase):
    # -- harness -------------------------------------------------------------

    def setUp(self):
        self.world = obs.reset_world()

        self.tmp = tempfile.mkdtemp(prefix="recorder-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # Keep the suite quick.
        self._saved_tunables = {name: getattr(recorder, name) for name in TUNABLES}
        recorder.MOVE_RETRY_BASE_SECONDS = 0.01
        recorder.MOVE_RETRY_MAX_SECONDS = 0.05
        recorder.SIZE_STABLE_POLL_SECONDS = 0.01
        recorder.REMUX_POLL_SECONDS = 0.02
        recorder.REMUX_TIMEOUT_SECONDS = 10.0
        recorder.WORKER_JOIN_TIMEOUT_SECONDS = 10.0

        self.clock = TimeShim()
        self._saved_time = recorder.time
        recorder.time = self.clock

        self._saved_shutil = recorder.shutil
        self._saved_config_path = recorder.get_config_path
        recorder.get_config_path = lambda: os.path.join(self.tmp, "cfg", "RecORDERConfig.json")

        recorder.core = None
        recorder.config_manager = None
        recorder.script_settings = None
        recorder._last_saved_selection = {}
        recorder._verbose_logging = False
        recorder._core_settings_signature = None
        recorder._deferred_logs.clear()

        self.addCleanup(self._teardown_script)

    def _teardown_script(self):
        try:
            if recorder.core is not None:
                recorder.script_unload()
            # A test may have created the shared worker without unloading.
            recorder.shutdown_file_move_worker(10.0)
        finally:
            recorder.time = self._saved_time
            recorder.shutil = self._saved_shutil
            recorder.get_config_path = self._saved_config_path
            for name, value in self._saved_tunables.items():
                setattr(recorder, name, value)

    def tearDown(self):
        # Anything a background thread logged is only visible after a tick.
        self.world.tick_timers()

        self.assertEqual(
            self.world.forbidden_calls, [], "RecORDER touched the user's recording/replay buffer"
        )
        errors = [line for level, line in self.world.log_lines if level == obs.LOG_ERROR]
        self.assertEqual(errors, [], "unexpected error log lines")

    # -- world builders ------------------------------------------------------

    def build_scene(self, title="ELDEN RING", executable="eldenring.exe", hooked=True,
                    source_id="game_capture", uuid="uuid-game"):
        scene = self.world.new_scene("Main")
        source = self.world.new_source(
            source_id, "Game Capture", uuid, hooked=hooked, title=title, executable=executable
        )
        scene.add(source)
        self.world.set_current_scene(scene)
        return scene, source

    def make_settings(self, **overrides):
        settings = obs.FakeData()
        recorder.script_defaults(settings)
        settings.values.update(overrides)
        return settings

    def load_script(self, settings=None, **overrides):
        settings = settings if settings is not None else self.make_settings(**overrides)
        recorder.script_load(settings)
        return settings

    def make_file(self, name, content=b"media-bytes", directory=None):
        path = os.path.join(directory or self.tmp, name)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def drain(self, timeout=10.0):
        self.assertTrue(recorder.core.mover.wait_idle(timeout), "file worker did not finish in time")
        # The worker's log lines are deferred; a timer tick delivers them.
        self.world.tick_timers()

    def save_replay(self, path):
        self.world.last_replay = path
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED)
        self.drain()

    def path_in(self, *parts):
        return os.path.join(self.tmp, *parts)

    def assertOrganized(self, source_path, *destination_parts):
        destination = self.path_in(*destination_parts)
        self.assertTrue(os.path.isfile(destination), "expected %s to exist" % destination)
        self.assertFalse(os.path.exists(source_path), "source file was not moved away")


# ============================================================================
# T1 - T4: title resolution
# ============================================================================


class TestTitleResolution(RecORDERTestCase):
    def test_T1_hooked_game_replay_goes_into_game_folder(self):
        self.build_scene()
        self.load_script()

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "ELDEN RING", "replay", "x.mkv")

    def test_T2_unhooked_three_seconds_ago_still_uses_the_game_folder(self):
        _, source = self.build_scene()
        self.load_script()

        self.world.hook(source, "ELDEN RING", "eldenring.exe")
        self.world.unhook(source)
        self.clock.advance(3)

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "ELDEN RING", "replay", "x.mkv")

    def test_T3_unhooked_ten_minutes_ago_uses_the_fallback_folder(self):
        _, source = self.build_scene()
        self.load_script()

        self.world.hook(source, "ELDEN RING", "eldenring.exe")
        self.world.unhook(source)
        self.clock.advance(600)

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "Any Recording", "replay", "x.mkv")

    def test_T4_game_already_hooked_before_the_script_loaded(self):
        scene, source = self.build_scene(title="Hades II", executable="Hades2.exe")
        self.load_script()

        # No hooked signal ever fired - only the live get_hooked query knows.
        self.assertEqual(source.handler.count("hooked"), 1)

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "Hades II", "replay", "x.mkv")

    def test_hook_memory_can_be_disabled(self):
        _, source = self.build_scene()
        self.load_script(**{recorder.PROPERTY_NAMES.HOOK_MEMORY_SECONDS: 0})

        self.world.hook(source, "ELDEN RING", "eldenring.exe")
        self.world.unhook(source)
        self.clock.advance(1)

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "Any Recording", "replay", "x.mkv")


# ============================================================================
# T5: name sanitization
# ============================================================================


class TestSanitization(RecORDERTestCase):
    def test_T5_sanitize_folder_name_rules(self):
        sanitize = recorder.sanitize_folder_name

        self.assertEqual(sanitize("原神"), "原神")
        self.assertEqual(sanitize("Counter-Strike 2"), "Counter-Strike 2")
        self.assertEqual(sanitize("S.T.A.L.K.E.R. 2"), "S.T.A.L.K.E.R. 2")
        self.assertEqual(sanitize("Half-Life: Alyx"), "Half-Life Alyx")
        self.assertEqual(sanitize('a<>:"/\\|?*b'), "ab")
        self.assertEqual(sanitize("Trailing dots..."), "Trailing dots")
        self.assertEqual(sanitize("  spaced   out  "), "spaced out")
        self.assertEqual(sanitize("line\nbreak\tgame"), "line break game")
        self.assertEqual(sanitize(""), "")
        self.assertEqual(sanitize(None), "")

        self.assertEqual(sanitize("NUL"), "_NUL")
        self.assertEqual(sanitize("nul"), "_nul")
        self.assertEqual(sanitize("COM1"), "_COM1")
        self.assertEqual(sanitize("LPT9"), "_LPT9")
        self.assertEqual(sanitize("NUL.txt"), "_NUL.txt")
        self.assertEqual(sanitize("CONTROL"), "CONTROL")

        long_title = "A" * 200
        self.assertEqual(len(sanitize(long_title)), 100)

    def test_T5_derive_folder_name_rules(self):
        derive = recorder.derive_folder_name

        self.assertEqual(derive("ELDEN RING", "eldenring.exe", "Any Recording"), "ELDEN RING")
        self.assertEqual(derive("Untitled", "eldenring.exe", "Any Recording"), "eldenring")
        self.assertEqual(derive("", "eldenring.exe", "Any Recording"), "eldenring")
        self.assertEqual(derive("Program Manager", "explorer.exe", "Any Recording"), "explorer")
        self.assertEqual(derive(None, None, "Any Recording"), "Any Recording")
        self.assertEqual(derive(None, None, ""), "Any Recording")
        self.assertEqual(derive(None, None, "My<>Vids?"), "MyVids")
        self.assertEqual(
            derive("ELDEN RING", "C:\\Games\\eldenring.exe", "Any Recording", True), "eldenring"
        )
        self.assertEqual(derive("原神", "YuanShen.exe", "Any Recording"), "原神")

    def test_T5_unicode_title_end_to_end(self):
        self.build_scene(title="原神", executable="YuanShen.exe")
        self.load_script()

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "原神", "replay", "x.mkv")

    def test_T5_hyphenated_title_end_to_end(self):
        self.build_scene(title="Counter-Strike 2", executable="cs2.exe")
        self.load_script()

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "Counter-Strike 2", "replay", "x.mkv")

    def test_folder_name_source_executable(self):
        self.build_scene(title="ELDEN RING", executable="eldenring.exe")
        self.load_script(
            **{recorder.PROPERTY_NAMES.FOLDER_NAME_SOURCE: recorder.FOLDER_NAME_SOURCES.EXECUTABLE}
        )

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "eldenring", "replay", "x.mkv")


# ============================================================================
# T6, T7: moving files
# ============================================================================


class TestFileMoving(RecORDERTestCase):
    def test_T6_existing_destination_is_never_overwritten(self):
        self.build_scene()
        self.load_script()

        existing = self.make_file(
            os.path.join("ELDEN RING", "replay", "x.mkv"), content=b"already-there"
        )

        replay = self.make_file("x.mkv", content=b"the-new-clip")
        self.save_replay(replay)

        self.assertOrganized(replay, "ELDEN RING", "replay", "x (1).mkv")
        with open(existing, "rb") as handle:
            self.assertEqual(handle.read(), b"already-there")
        with open(self.path_in("ELDEN RING", "replay", "x (1).mkv"), "rb") as handle:
            self.assertEqual(handle.read(), b"the-new-clip")

    def test_T7_sharing_violation_is_retried(self):
        self.build_scene()
        self.load_script()

        flaky = FlakyShutil(failures=2)
        recorder.shutil = flaky

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertGreaterEqual(flaky.calls, 3)
        self.assertOrganized(replay, "ELDEN RING", "replay", "x.mkv")

    def test_move_gives_up_after_the_attempt_budget(self):
        self.build_scene()
        self.load_script()

        recorder.shutil = FlakyShutil(failures=recorder.MOVE_MAX_ATTEMPTS + 5)

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        # The clip stays where it was, and we said so out loud.
        self.assertTrue(os.path.exists(replay))
        messages = [line for _level, line in self.world.log_lines]
        self.assertTrue(any("Could not move" in line for line in messages))
        # Keep tearDown's "no error logs" rule happy - this one is expected.
        self.world.log_lines = [
            (level, line) for level, line in self.world.log_lines if "Could not move" not in line
        ]

    def test_screenshots_use_their_own_folder(self):
        self.build_scene()
        self.load_script()

        screenshot = self.make_file("shot.png")
        self.world.last_screenshot = screenshot
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_SCREENSHOT_TAKEN)
        self.drain()

        self.assertOrganized(screenshot, "ELDEN RING", "screenshot", "shot.png")

    def test_title_as_prefix(self):
        self.build_scene()
        self.load_script(**{recorder.PROPERTY_NAMES.TITLE_AS_PREFIX: True})

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "ELDEN RING", "replay", "ELDEN RING - x.mkv")


# ============================================================================
# T8, T9: lifecycle
# ============================================================================


class TestLifecycle(RecORDERTestCase):
    def test_T8_script_update_never_stops_an_active_recording(self):
        _, source = self.build_scene()
        settings = self.load_script()

        recording = self.make_file("session.mkv")
        self.world.last_recording = recording
        self.world.recording_active = True
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STARTED)

        self.world.hook(source, "ELDEN RING", "eldenring.exe")
        self.world.unhook(source)
        self.clock.advance(2)

        settings.values[recorder.PROPERTY_NAMES.REPLAY_FOLDER_NAME] = "clips"
        recorder.script_update(settings)

        self.assertEqual(self.world.forbidden_calls, [])
        self.assertTrue(self.world.recording_active)

        # The memory of the last hook survived the rebuild...
        self.assertEqual(recorder.core.hook_info.title, "ELDEN RING")

        # ...and the in-progress recording was adopted, not forgotten.
        self.assertEqual(recorder.core.recording_state.last_file_path, recording)

        replay = self.make_file("x.mkv")
        self.save_replay(replay)
        self.assertOrganized(replay, "ELDEN RING", "clips", "x.mkv")

        self.world.recording_active = False
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED)
        self.drain()
        self.assertOrganized(recording, "ELDEN RING", "session.mkv")

    def test_T9_signals_are_connected_once_and_cleaned_up(self):
        _, source = self.build_scene()
        settings = self.load_script()

        self.assertEqual(source.handler.count("hooked"), 1)
        self.assertEqual(source.handler.count("unhooked"), 1)

        recorder.script_update(settings)
        recorder.script_update(settings)

        self.assertEqual(source.handler.count("hooked"), 1)
        self.assertEqual(source.handler.count("unhooked"), 1)

        self.world.last_recording = self.make_file("session.mkv")
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STARTED)
        self.assertEqual(self.world.recording_output.handler.count("file_changed"), 1)

        recorder.script_unload()

        self.assertEqual(source.handler.count("hooked"), 0)
        self.assertEqual(source.handler.count("unhooked"), 0)
        self.assertEqual(self.world.recording_output.handler.count("file_changed"), 0)
        self.assertEqual(self.world.event_callbacks, [])
        self.assertEqual(self.world.source_refs, 0)
        self.assertEqual(self.world.output_refs, 0)
        self.assertEqual(self.world.item_lists_outstanding, 0)

    def test_signal_callbacks_are_plain_functions(self):
        # The real binding silently ignores bound methods; the stub does too, so
        # a regression here would show up as a missing connection.
        _, source = self.build_scene()
        self.load_script()

        for callback in source.handler.callbacks("hooked"):
            self.assertTrue(callable(callback))
        self.assertEqual(source.handler.count("hooked"), 1)

        self.world.hook(source, "Deep Rock Galactic", "FSD-Win64-Shipping.exe")
        self.assertEqual(recorder.core.hook_info.title, "Deep Rock Galactic")

    def test_scene_collection_change_detaches_and_rediscovers(self):
        scene, source = self.build_scene()
        self.load_script()

        self.world.fire_event(obs.OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGING)
        self.assertEqual(source.handler.count("hooked"), 0)
        self.assertIsNone(recorder.core.hook_manager.source_uuid)

        # A brand new collection with a brand new source.
        new_scene = self.world.new_scene("Fresh")
        new_source = self.world.new_source(
            "game_capture", "Game Capture", "uuid-new", hooked=True, title="DOOM", executable="doom.exe"
        )
        new_scene.add(new_source)
        self.world.set_current_scene(new_scene)

        self.world.fire_event(obs.OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGED)
        self.assertEqual(new_source.handler.count("hooked"), 1)

        replay = self.make_file("x.mkv")
        self.save_replay(replay)
        self.assertOrganized(replay, "DOOM", "replay", "x.mkv")


# ============================================================================
# T10, T11: discovery
# ============================================================================


class TestDiscovery(RecORDERTestCase):
    def _nested_world(self):
        root = self.world.new_scene("Main")
        nested = self.world.new_scene("Nested")
        group_scene = self.world.new_scene("Group contents")
        group_source = self.world.new_source("group", "My Group", "uuid-group")

        game = self.world.new_source(
            "game_capture",
            "Deep Game Capture",
            "uuid-deep",
            hooked=True,
            title="Baldurs Gate 3",
            executable="bg3.exe",
        )

        group_scene.add(game)
        nested.add_group(group_source, group_scene)
        root.add(nested.source)
        self.world.set_current_scene(root)
        return root, game

    def test_T10_source_inside_a_group_inside_a_nested_scene(self):
        _, game = self._nested_world()
        self.load_script()

        self.assertEqual(recorder.core.hook_manager.source_uuid, "uuid-deep")
        self.assertEqual(game.handler.count("hooked"), 1)

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "Baldurs Gate 3", "replay", "x.mkv")

    def test_T11_source_recreated_with_a_new_uuid(self):
        scene, source = self.build_scene()
        self.load_script()
        self.assertEqual(recorder.core.hook_manager.source_uuid, "uuid-game")

        # User deleted the capture and made a new one.
        scene.items = []
        self.world.remove_source(source)

        replacement = self.world.new_source(
            "game_capture",
            "Game Capture 2",
            "uuid-game-2",
            hooked=True,
            title="Helldivers 2",
            executable="helldivers2.exe",
        )
        scene.add(replacement)

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertEqual(recorder.core.hook_manager.source_uuid, "uuid-game-2")
        self.assertOrganized(replay, "Helldivers 2", "replay", "x.mkv")

    def test_auto_detect_prefers_a_hooked_source(self):
        scene = self.world.new_scene("Main")
        idle_game = self.world.new_source("game_capture", "Idle", "uuid-idle", hooked=False)
        hooked_window = self.world.new_source(
            "window_capture", "Window", "uuid-window", hooked=True, title="Factorio",
            executable="factorio.exe",
        )
        scene.add(idle_game)
        scene.add(hooked_window)
        self.world.set_current_scene(scene)

        self.load_script()
        self.assertEqual(recorder.core.hook_manager.source_uuid, "uuid-window")

    def test_non_hookable_selection_is_reported_and_auto_detected(self):
        scene, game = self.build_scene()
        display = self.world.new_source("monitor_capture", "Display", "uuid-display")
        scene.add(display)

        self.load_script(**{recorder.PROPERTY_NAMES.SOURCE_SELECTOR: "uuid-display"})

        messages = [line for _level, line in self.world.log_lines]
        self.assertTrue(any("cannot report hooked windows" in line for line in messages))
        self.assertEqual(recorder.core.hook_manager.source_uuid, "uuid-game")

    def test_source_selector_lists_auto_detect_and_unknown_selection(self):
        self.build_scene()
        self.load_script(**{recorder.PROPERTY_NAMES.SOURCE_SELECTOR: "uuid-from-another-scene"})

        props = recorder.script_properties()
        group = props.properties["core_group"].group
        selector = group.properties[recorder.PROPERTY_NAMES.SOURCE_SELECTOR]

        values = [value for _name, value in selector.items]
        self.assertEqual(values[0], "")
        self.assertIn("uuid-game", values)
        self.assertIn("uuid-from-another-scene", values)

    def test_linux_xcomposite_source_is_supported(self):
        scene = self.world.new_scene("Main")
        source = self.world.new_source(
            "xcomposite_input", "Window Capture (XComposite)", "uuid-x11", hooked=True,
            title="SuperTuxKart", executable=None,
        )
        scene.add(source)
        self.world.set_current_scene(scene)

        self.load_script()

        replay = self.make_file("x.mkv")
        self.save_replay(replay)
        self.assertOrganized(replay, "SuperTuxKart", "replay", "x.mkv")


# ============================================================================
# T12: recording splits
# ============================================================================


class TestRecordingSplits(RecORDERTestCase):
    def test_T12_every_split_is_organized_exactly_once(self):
        self.build_scene()
        self.load_script()

        first = self.make_file("part1.mkv")
        second = self.make_file("part2.mkv")
        third = self.make_file("part3.mkv")

        self.world.last_recording = first
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STARTED)

        self.world.fire_file_changed(second)
        self.world.fire_file_changed(third)

        # A duplicate signal for a file we already handled must be ignored.
        self.world.fire_file_changed(third)

        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED)
        self.drain()

        game_dir = self.path_in("ELDEN RING")
        self.assertEqual(sorted(os.listdir(game_dir)), ["part1.mkv", "part2.mkv", "part3.mkv"])
        for path in (first, second, third):
            self.assertFalse(os.path.exists(path))

    def test_F1_file_changed_without_a_path_is_ignored(self):
        # The signal arrives on the muxer thread, where obs_frontend_* is off
        # limits - so an empty next_file leaves the state exactly as it was.
        self.build_scene()
        self.load_script()

        first = self.make_file("part1.mkv")
        second = self.make_file("part2.mkv")

        self.world.last_recording = first
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STARTED)

        self.world.last_recording = second
        self.world.fire_signal(self.world.recording_output, "file_changed")

        self.assertEqual(recorder.core.recording_state.last_file_path, first)
        self.assertFalse(os.path.isdir(self.path_in("ELDEN RING")))
        self.assertTrue(os.path.exists(second))

        messages = [line for _level, line in self.world.log_lines]
        self.assertTrue(
            any("carried no 'next_file'" in line for line in messages),
            "the ignored split should be logged as a warning",
        )

        # The final file is still organized by the (UI thread) stop handler.
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED)
        self.drain()
        self.assertOrganized(first, "ELDEN RING", "part1.mkv")

    def test_F1_the_stub_rejects_frontend_calls_from_the_muxer_thread(self):
        # Guard for the guard: prove the stub really would fail the suite.
        self.build_scene()
        self.load_script()

        def peeking_callback(calldata):
            obs.obs_frontend_get_last_recording()

        self.world.recording_output.handler.connect("file_changed", peeking_callback)

        with self.assertRaises(AssertionError):
            self.world.fire_file_changed("part2.mkv")

        self.assertEqual(self.world.forbidden_calls, ["obs_frontend_get_last_recording"])
        self.assertEqual(self.world.current_thread_role, "ui")
        self.world.forbidden_calls = []


# ============================================================================
# T13: fallback naming
# ============================================================================


class TestFallbackName(RecORDERTestCase):
    def test_T13_empty_fallback_name_uses_the_default(self):
        self.build_scene(hooked=False, title=None, executable=None)
        self.load_script(**{recorder.PROPERTY_NAMES.FALLBACK_WINDOW_NAME: ""})

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "Any Recording", "replay", "x.mkv")

    def test_T13_illegal_fallback_name_is_sanitized(self):
        self.build_scene(hooked=False, title=None, executable=None)
        self.load_script(**{recorder.PROPERTY_NAMES.FALLBACK_WINDOW_NAME: 'My<>:"/\\|?*Clips.'})

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "MyClips", "replay", "x.mkv")

    def test_T13_illegal_replay_folder_name_is_sanitized(self):
        self.build_scene()
        self.load_script(**{recorder.PROPERTY_NAMES.REPLAY_FOLDER_NAME: "clips:/best"})

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "ELDEN RING", "clipsbest", "x.mkv")


# ============================================================================
# T14: auto-remux
# ============================================================================


class TestAutoRemux(RecORDERTestCase):
    def test_T14_original_and_remuxed_file_are_both_moved(self):
        self.build_scene()
        self.world.profile_config = obs.FakeConfig({("Video", "AutoRemux"): True})
        self.load_script()

        recording = self.make_file("session.mkv")
        remuxed_path = os.path.join(self.tmp, "session.mp4")

        def produce_remuxed():
            with open(remuxed_path, "wb") as handle:
                handle.write(b"remuxed-bytes")

        timer = threading.Timer(0.3, produce_remuxed)
        timer.start()
        self.addCleanup(timer.cancel)

        self.world.last_recording = recording
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STARTED)
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED)
        self.drain()

        self.assertOrganized(recording, "ELDEN RING", "session.mkv")
        self.assertTrue(os.path.isfile(self.path_in("ELDEN RING", "session.mp4")))
        self.assertFalse(os.path.exists(remuxed_path))

    def test_remux_timeout_still_moves_the_original(self):
        self.build_scene()
        self.world.profile_config = obs.FakeConfig({("Video", "AutoRemux"): True})
        recorder.REMUX_TIMEOUT_SECONDS = 0.15
        self.load_script()

        recording = self.make_file("session.mkv")
        self.world.last_recording = recording
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STARTED)
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED)
        self.drain()

        self.assertOrganized(recording, "ELDEN RING", "session.mkv")

    def test_replays_never_wait_for_a_remux(self):
        self.build_scene()
        self.world.profile_config = obs.FakeConfig({("Video", "AutoRemux"): True})
        recorder.REMUX_TIMEOUT_SECONDS = 30.0
        self.load_script()

        started = time.monotonic()
        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertLess(time.monotonic() - started, 5.0)
        self.assertOrganized(replay, "ELDEN RING", "replay", "x.mkv")

    def test_splits_use_the_flag_cached_on_the_ui_thread(self):
        # file_changed fires on the muxer thread, so the AutoRemux flag must
        # already be known by then - it must not be read from there.
        self.build_scene()
        self.world.profile_config = obs.FakeConfig({("Video", "AutoRemux"): True})
        recorder.REMUX_TIMEOUT_SECONDS = 0.15
        self.load_script()

        first = self.make_file("part1.mkv")
        second = self.make_file("part2.mkv")

        self.world.last_recording = first
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STARTED)
        self.assertTrue(recorder.core.organizer.auto_remux_enabled)

        self.world.fire_file_changed(second)
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED)
        self.drain()

        self.assertEqual(
            sorted(os.listdir(self.path_in("ELDEN RING"))), ["part1.mkv", "part2.mkv"]
        )

    def test_auto_remux_flag_is_read_from_the_profile(self):
        self.world.profile_config = obs.FakeConfig({("Video", "AutoRemux"): True})
        self.assertTrue(recorder.is_auto_remux_enabled())
        self.world.profile_config = obs.FakeConfig()
        self.assertFalse(recorder.is_auto_remux_enabled())
        self.world.profile_config = None
        self.assertFalse(recorder.is_auto_remux_enabled())


# ============================================================================
# T15: date mode
# ============================================================================


class TestOrganizationModes(RecORDERTestCase):
    def test_T15_date_mode_adds_a_date_folder(self):
        from datetime import datetime

        self.build_scene()
        self.load_script(
            **{
                recorder.PROPERTY_NAMES.ORGANIZATION_MODE:
                    recorder.AVAILABLE_ORGANIZATION_MODES.DATE_BASED
            }
        )

        replay = self.make_file("x.mkv")
        expected_date = datetime.fromtimestamp(os.path.getctime(replay)).strftime("%y-%m-%d")

        self.save_replay(replay)

        self.assertOrganized(replay, "ELDEN RING", "replay", expected_date, "x.mkv")

    def test_unknown_mode_falls_back_to_basic(self):
        self.build_scene()
        self.load_script(**{recorder.PROPERTY_NAMES.ORGANIZATION_MODE: "scene_based"})

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "ELDEN RING", "replay", "x.mkv")

    def test_recordings_have_no_media_subfolder(self):
        self.build_scene()
        self.load_script()

        recording = self.make_file("session.mkv")
        self.world.last_recording = recording
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STARTED)
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED)
        self.drain()

        self.assertOrganized(recording, "ELDEN RING", "session.mkv")


# ============================================================================
# Misc: settings persistence and the update button
# ============================================================================


class TestConfigAndUpdates(RecORDERTestCase):
    def test_scene_mapping_is_only_written_when_it_changes(self):
        scene, source = self.build_scene()
        settings = self.load_script(**{recorder.PROPERTY_NAMES.SOURCE_SELECTOR: "uuid-game"})

        writes = []
        original = recorder.config_manager.saveSourceForScene

        def counting_save(collection, scene_name, uuid):
            writes.append((collection, scene_name, uuid))
            return original(collection, scene_name, uuid)

        recorder.config_manager.saveSourceForScene = counting_save

        recorder.script_update(settings)
        recorder.script_update(settings)
        self.assertEqual(writes, [])

        settings.values[recorder.PROPERTY_NAMES.SOURCE_SELECTOR] = "uuid-other"
        self.world.new_source("game_capture", "Other", "uuid-other")
        recorder.script_update(settings)
        self.assertEqual(len(writes), 1)

    def test_saved_mapping_is_applied_on_scene_change(self):
        scene, source = self.build_scene()
        other = self.world.new_source(
            "game_capture", "Second capture", "uuid-second", hooked=True, title="Terraria",
            executable="Terraria.exe",
        )
        scene.add(other)

        self.load_script()
        recorder.config_manager.saveSourceForScene(
            self.world.scene_collection, "Main", "uuid-second"
        )

        self.world.fire_event(obs.OBS_FRONTEND_EVENT_SCENE_CHANGED)
        self.assertEqual(recorder.core.hook_manager.source_uuid, "uuid-second")

        replay = self.make_file("x.mkv")
        self.save_replay(replay)
        self.assertOrganized(replay, "Terraria", "replay", "x.mkv")

    def test_update_button_signature_and_version_compare(self):
        self.build_scene()
        self.load_script()

        self.assertTrue(recorder.is_update_available("3.2.0", "v3.3.0"))
        self.assertFalse(recorder.is_update_available("3.2.0", "v3.2.0"))
        self.assertFalse(recorder.is_update_available("3.2.0", "3.2.0"))
        self.assertFalse(recorder.is_update_available("3.2.0", None))

        original = recorder.get_latest_release_tag
        recorder.get_latest_release_tag = lambda: "v9.9.9"
        try:
            props = recorder.script_properties()
            group = props.properties["update_group"].group
            button = group.properties["check_updates_button"]
            self.assertIsNone(button.modified_callback)

            self.assertTrue(button.callback(group, button))
            info = group.properties["version_info"]
            self.assertTrue(info.visible)
            self.assertIn("9.9.9", info.description)
        finally:
            recorder.get_latest_release_tag = original

    def test_script_entry_points_never_raise(self):
        self.build_scene()
        settings = self.load_script()

        # A source that explodes on every call must not take the script down.
        def boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        original = obs.obs_frontend_get_current_scene
        obs.obs_frontend_get_current_scene = boom
        try:
            recorder.script_update(settings)
            recorder.frontend_event_callback(obs.OBS_FRONTEND_EVENT_SCENE_CHANGED)
            self.assertIsInstance(recorder.script_properties(), obs.FakeProperties)
        finally:
            obs.obs_frontend_get_current_scene = original

        self.world.log_lines = []


# ============================================================================
# F2: the hook memory is scoped to a scene collection
# ============================================================================


class TestHookMemoryScope(RecORDERTestCase):
    def test_F2_scene_collection_change_forgets_the_remembered_hook(self):
        scene, source = self.build_scene()
        self.load_script()

        self.world.hook(source, "ELDEN RING", "eldenring.exe")
        self.assertEqual(recorder.core.hook_info.title, "ELDEN RING")

        self.world.fire_event(obs.OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGING)
        self.assertIsNone(recorder.core.hook_info.title)
        self.assertIsNone(recorder.core.hook_info.executable)
        self.assertFalse(recorder.core.hook_info.has_memory())

        # Collection B has its own capture source, hooked to nothing.
        self.world.scene_collection = "Collection B"
        fresh_scene = self.world.new_scene("Fresh")
        idle = self.world.new_source("game_capture", "Game Capture", "uuid-b", hooked=False)
        fresh_scene.add(idle)
        self.world.set_current_scene(fresh_scene)

        self.world.fire_event(obs.OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGED)
        self.assertEqual(recorder.core.hook_manager.source_uuid, "uuid-b")

        self.clock.advance(3)
        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "Any Recording", "replay", "x.mkv")

    def test_F2_a_plain_scene_change_keeps_the_remembered_hook(self):
        # Switching to an overlay/BRB scene mid-game must not lose the game.
        scene, source = self.build_scene()
        self.load_script()

        self.world.hook(source, "ELDEN RING", "eldenring.exe")
        self.world.unhook(source)
        self.clock.advance(3)

        self.world.fire_event(obs.OBS_FRONTEND_EVENT_SCENE_CHANGED)
        self.assertEqual(recorder.core.hook_info.title, "ELDEN RING")

        replay = self.make_file("x.mkv")
        self.save_replay(replay)

        self.assertOrganized(replay, "ELDEN RING", "replay", "x.mkv")


# ============================================================================
# F3: the file-move worker is shared and outlives a rebuild
# ============================================================================


class TestSharedFileMoveWorker(RecORDERTestCase):
    def test_F3_a_job_in_flight_survives_a_rebuild(self):
        self.build_scene()
        settings = self.load_script()

        blocker = BlockingShutil(failures=2)
        recorder.shutil = blocker

        worker = recorder.core.mover
        replay = self.make_file("x.mkv")
        self.world.last_replay = replay
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED)

        self.assertTrue(blocker.first_call.wait(5.0), "the worker never started the move")

        started = time.monotonic()
        settings.values[recorder.PROPERTY_NAMES.REPLAY_FOLDER_NAME] = "clips"
        recorder.script_update(settings)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0, "script_update blocked on the file worker")
        self.assertIs(recorder.core.mover, worker, "the worker must be shared across rebuilds")
        self.assertIs(recorder._file_move_worker, worker)
        self.assertTrue(worker._thread.is_alive(), "the rebuild killed the worker")

        blocker.release.set()
        self.drain()

        # The job was self-contained: it still lands where it was addressed at
        # submit time, not in the folder the new settings would have picked.
        self.assertOrganized(replay, "ELDEN RING", "replay", "x.mkv")
        self.assertFalse(os.path.exists(self.path_in("ELDEN RING", "clips")))

    def test_F3_core_shutdown_does_not_join_the_worker(self):
        self.build_scene()
        self.load_script()

        worker = recorder.core.mover
        recorder.core.shutdown()

        self.assertFalse(worker._stopping, "core.shutdown() must not stop the shared worker")
        self.assertIs(recorder._file_move_worker, worker)

    def test_F3_script_unload_drains_and_retires_the_worker(self):
        self.build_scene()
        self.load_script()

        recording = self.make_file("session.mkv")
        self.world.last_recording = recording
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STARTED)
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED)

        worker = recorder.core.mover
        recorder.script_unload()

        self.assertTrue(worker._stopping)
        self.assertIsNone(recorder._file_move_worker)
        self.assertTrue(os.path.isfile(self.path_in("ELDEN RING", "session.mkv")))
        self.assertEqual(self.world.timers, [], "the log flush timer was left registered")

    def test_F3_an_exception_in_a_job_does_not_kill_the_worker(self):
        self.build_scene()
        self.load_script()

        original = recorder._wait_until_size_is_stable
        self.addCleanup(setattr, recorder, "_wait_until_size_is_stable", original)
        seen = {"count": 0}

        def exploding(path):
            seen["count"] += 1
            if seen["count"] == 1:
                raise RuntimeError("kaboom")
            return original(path)

        recorder._wait_until_size_is_stable = exploding

        doomed = self.make_file("boom.mkv")
        self.save_replay(doomed)
        self.assertTrue(os.path.exists(doomed), "the crashing job should not have moved anything")

        # The very next job goes through on the same thread.
        good = self.make_file("ok.mkv")
        self.save_replay(good)
        self.assertOrganized(good, "ELDEN RING", "replay", "ok.mkv")

        messages = [line for _level, line in self.world.log_lines]
        self.assertTrue(any("file move worker" in line for line in messages))
        self.world.log_lines = [
            (level, line) for level, line in self.world.log_lines
            if "file move worker" not in line
        ]


# ============================================================================
# F4: a source hooked mid-recording still names the final file
# ============================================================================


class TestLateDiscovery(RecORDERTestCase):
    def test_F4_source_added_mid_recording_is_discovered_before_the_stop(self):
        scene = self.world.new_scene("Main")
        self.world.set_current_scene(scene)
        self.load_script()

        self.assertIsNone(recorder.core.hook_manager.source_uuid)

        recording = self.make_file("session.mkv")
        self.world.last_recording = recording
        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STARTED)

        # The user drops a Game Capture into the scene while recording.
        late = self.world.new_source(
            "game_capture", "Game Capture", "uuid-late", hooked=True, title="DOOM",
            executable="doom.exe",
        )
        scene.add(late)

        self.world.fire_event(obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED)
        self.drain()

        self.assertEqual(recorder.core.hook_manager.source_uuid, "uuid-late")
        self.assertOrganized(recording, "DOOM", "session.mkv")


# ============================================================================
# F5: logging from a background thread is deferred to the UI thread
# ============================================================================


class TestDeferredLogging(RecORDERTestCase):
    def test_F5_script_load_registers_a_plain_function_timer(self):
        self.build_scene()
        self.load_script()

        callbacks = [callback for callback, _interval in self.world.timers]
        self.assertIn(recorder._flush_deferred_logs, callbacks)
        self.assertEqual(
            [interval for _callback, interval in self.world.timers],
            [recorder.LOG_FLUSH_INTERVAL_MS],
        )
        for callback in callbacks:
            self.assertTrue(inspect.isfunction(callback), "OBS timers need plain functions")

    def test_F5_off_thread_log_lines_wait_for_a_tick_and_keep_their_order(self):
        self.build_scene()
        self.load_script()
        self.world.tick_timers()
        self.world.log_lines = []

        def from_a_worker_thread():
            recorder.log("info", "first from the worker")
            recorder.log("warning", "second from the worker")

        thread = threading.Thread(target=from_a_worker_thread)
        thread.start()
        thread.join(5.0)
        self.assertFalse(thread.is_alive())

        self.assertEqual(self.world.log_lines, [], "script_log was called off the UI thread")

        self.world.tick_timers()
        self.assertEqual(
            self.world.log_lines,
            [
                (obs.LOG_INFO, "[RecORDER] first from the worker"),
                (obs.LOG_WARNING, "[RecORDER] second from the worker"),
            ],
        )

        # A second tick has nothing left to say.
        self.world.tick_timers()
        self.assertEqual(len(self.world.log_lines), 2)

    def test_F5_main_thread_log_lines_are_written_immediately(self):
        self.build_scene()
        self.load_script()
        self.world.log_lines = []

        recorder.log("info", "straight through")
        self.assertEqual(self.world.log_lines, [(obs.LOG_INFO, "[RecORDER] straight through")])


# ============================================================================
# F6: no rebuild when nothing changed
# ============================================================================


class TestRebuildAvoidance(RecORDERTestCase):
    def test_F6_identical_settings_do_not_rebuild_the_core(self):
        _, source = self.build_scene()

        builds = []
        original_class = recorder.RecORDER

        def counting_core(*args, **kwargs):
            instance = original_class(*args, **kwargs)
            builds.append(instance)
            return instance

        recorder.RecORDER = counting_core
        self.addCleanup(setattr, recorder, "RecORDER", original_class)

        settings = self.load_script()
        self.assertEqual(len(builds), 1, "script_load should build exactly one core")
        self.assertEqual(source.handler.count("hooked"), 1)

        # This is what OBS does right after a successful script_load.
        recorder.script_update(settings)
        recorder.script_update(settings)

        self.assertEqual(len(builds), 1, "identical settings must not rebuild the core")
        self.assertIs(recorder.core, builds[0])
        self.assertEqual(source.handler.count("hooked"), 1)
        self.assertEqual(source.handler.count("unhooked"), 1)

        # A real change still rebuilds.
        settings.values[recorder.PROPERTY_NAMES.REPLAY_FOLDER_NAME] = "clips"
        recorder.script_update(settings)
        self.assertEqual(len(builds), 2)
        self.assertIs(recorder.core, builds[1])
        self.assertEqual(source.handler.count("hooked"), 1)

    def test_F6_every_property_is_part_of_the_signature(self):
        self.build_scene()
        settings = self.load_script()

        baseline = recorder.settings_signature(settings)
        changes = {
            recorder.PROPERTY_NAMES.SOURCE_SELECTOR: "uuid-other",
            recorder.PROPERTY_NAMES.ORGANIZATION_MODE: "date_based",
            recorder.PROPERTY_NAMES.TITLE_AS_PREFIX: True,
            recorder.PROPERTY_NAMES.ENABLE_REPLAY_ORGANIZATION: False,
            recorder.PROPERTY_NAMES.ENABLE_SCREENSHOT_ORGANIZATION: False,
            recorder.PROPERTY_NAMES.FALLBACK_WINDOW_NAME: "Other",
            recorder.PROPERTY_NAMES.REPLAY_FOLDER_NAME: "clips",
            recorder.PROPERTY_NAMES.SCREENSHOT_FOLDER_NAME: "shots",
            recorder.PROPERTY_NAMES.HOOK_MEMORY_SECONDS: 42,
            recorder.PROPERTY_NAMES.FOLDER_NAME_SOURCE: "executable",
            recorder.PROPERTY_NAMES.VERBOSE_LOGGING: True,
        }

        for key, value in changes.items():
            probe = self.make_settings(**{key: value})
            self.assertNotEqual(
                recorder.settings_signature(probe), baseline, "%s is not in the signature" % key
            )


# ============================================================================
# F7: scene walking and the processed-path guard
# ============================================================================


class TestNits(RecORDERTestCase):
    def test_F7_the_root_scene_is_walked_only_once(self):
        scene, _source = self.build_scene()
        scene.add(scene.source)  # a scene that contains itself

        walked = []
        original = obs.obs_scene_enum_items

        def counting(target):
            walked.append(target)
            return original(target)

        obs.obs_scene_enum_items = counting
        try:
            found = recorder.enumerate_hookable_sources()
        finally:
            obs.obs_scene_enum_items = original

        self.assertEqual([entry["uuid"] for entry in found], ["uuid-game"])
        self.assertEqual(len(walked), 1, "the root scene was walked more than once")
        self.assertEqual(self.world.item_lists_outstanding, 0)
        self.assertEqual(self.world.source_refs, 0)

    def test_F7_processed_paths_are_guarded_by_a_lock(self):
        self.build_scene()
        self.load_script()

        manager = recorder.core.recording_manager
        self.assertIsInstance(manager._processed_lock, type(threading.Lock()))

        organized = []
        organized_lock = threading.Lock()

        def counting_process(path):
            with organized_lock:
                organized.append(path)

        recorder.core.organizer.processRecording = counting_process

        racers = 8
        barrier = threading.Barrier(racers)

        def racer():
            barrier.wait(5.0)
            manager._RecordingManager__process("part2.mkv")

        threads = [threading.Thread(target=racer) for _ in range(racers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5.0)
            self.assertFalse(thread.is_alive())

        self.assertEqual(organized, ["part2.mkv"], "a split was organized more than once")


# ============================================================================
# F8: the update button toggles
# ============================================================================


class TestUpdateButtonToggle(RecORDERTestCase):
    def test_F8_second_press_hides_the_info_without_a_network_call(self):
        self.build_scene()
        self.load_script()

        queries = []

        def fake_tag():
            queries.append(1)
            return "v9.9.9"

        original = recorder.get_latest_release_tag
        recorder.get_latest_release_tag = fake_tag
        try:
            props = recorder.script_properties()
            group = props.properties["update_group"].group
            button = group.properties["check_updates_button"]
            info = group.properties["version_info"]

            self.assertFalse(info.visible)

            self.assertTrue(button.callback(group, button))
            self.assertTrue(info.visible)
            self.assertIn("9.9.9", info.description)
            self.assertEqual(len(queries), 1)

            # Pressing again just hides the text - GitHub is left alone.
            self.assertTrue(button.callback(group, button))
            self.assertFalse(info.visible)
            self.assertEqual(len(queries), 1)

            # ...and a third press asks again.
            self.assertTrue(button.callback(group, button))
            self.assertTrue(info.visible)
            self.assertEqual(len(queries), 2)
        finally:
            recorder.get_latest_release_tag = original


if __name__ == "__main__":
    unittest.main()
