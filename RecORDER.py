"""
RecORDER - organize OBS media (recordings, replay buffer clips, screenshots)
into per-game subfolders, ShadowPlay style.

Original author: oxypatic (61553947+oxypatic@users.noreply.github.com) - https://github.com/oxypatic/RecORDER
This file is a fork (v3.2.0) maintained at https://github.com/Junnn888/obs-replay-folder-organisation
Licensed under the GNU AGPL v3.0, like the original.
"""

import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from typing import Optional
from urllib.request import urlopen

import obspython as obs  # type: ignore


# ============================================================================
# LITERALS CLASSES
# ============================================================================


class BASE_CONSTANTS:
    VERSION = "3.2.0"
    PYTHON_VERSION = sys.version_info


class PROPERTY_NAMES:
    FALLBACK_WINDOW_NAME = "fallback_window_title"
    REPLAY_FOLDER_NAME = "replay_folder_name"
    SCREENSHOT_FOLDER_NAME = "screenshot_folder_name"
    ORGANIZATION_MODE = "organization_mode"
    TITLE_AS_PREFIX = "title_as_prefix"
    ENABLE_REPLAY_ORGANIZATION = "enable_replay_organization"
    ENABLE_SCREENSHOT_ORGANIZATION = "enable_screenshot_organization"
    SOURCE_SELECTOR = "source_selector"
    # Added in 3.2.0
    HOOK_MEMORY_SECONDS = "hook_memory_seconds"
    FOLDER_NAME_SOURCE = "folder_name_source"
    VERBOSE_LOGGING = "verbose_logging"


class OBS_EVENT_NAMES:
    HOOKED_SIGNAL_NAME = "hooked"
    UNHOOKED_SIGNAL_NAME = "unhooked"
    TITLE_CALLDATA_NAME_WINDOWS = "title"
    TITLE_CALLDATA_NAME_XCOMPOSITE = "name"
    EXECUTABLE_CALLDATA_NAME = "executable"
    GET_HOOKED_PROCEDURE_NAME = "get_hooked"
    FILE_CHANGED_SIGNAL_NAME = "file_changed"
    NEXT_FILE_CALLDATA_NAME = "next_file"


class SUPPORTED_MEDIAFILE_TYPES:
    RECORDING = "recording"
    REPLAY = "replay"
    SCREENSHOT = "screenshot"


class SUPPORTED_SOURCE_TYPES:
    GAME_CAPTURE = "game_capture"
    WINDOW_CAPTURE = "window_capture"
    XCOMPOSITE_CAPTURE = "xcomposite_input"  # Linux (X11) window capture

    ALL = (GAME_CAPTURE, WINDOW_CAPTURE, XCOMPOSITE_CAPTURE)


class AVAILABLE_ORGANIZATION_MODES:
    BASIC = "basic"
    DATE_BASED = "date_based"


class FOLDER_NAME_SOURCES:
    WINDOW_TITLE = "title"
    EXECUTABLE = "executable"


class RESOLUTION_TIERS:
    LIVE = "live hook"
    MEMORY = "remembered hook"
    FALLBACK = "fallback name"


# ---------------------------------------------------------------------------
# Tunables. Kept at module level on purpose - the test-suite overrides them
# to keep the suite fast, and a user can tweak them in a pinch.
# ---------------------------------------------------------------------------

MOVE_MAX_ATTEMPTS = 8
MOVE_RETRY_BASE_SECONDS = 0.25
MOVE_RETRY_MAX_SECONDS = 3.0

SIZE_STABLE_POLL_SECONDS = 0.25
SIZE_STABLE_MAX_SAMPLES = 40

WORKER_JOIN_TIMEOUT_SECONDS = 5.0

LOG_FLUSH_INTERVAL_MS = 250

REMUX_POLL_SECONDS = 2.0
REMUX_TIMEOUT_SECONDS = 600.0

DEFAULT_HOOK_MEMORY_SECONDS = 120
DEFAULT_FALLBACK_FOLDER_NAME = "Any Recording"
DEFAULT_REPLAY_FOLDER_NAME = "replay"
DEFAULT_SCREENSHOT_FOLDER_NAME = "screenshot"
MAX_FOLDER_NAME_LENGTH = 100

UPDATE_CHECK_URL = "https://api.github.com/repos/Junnn888/obs-replay-folder-organisation/releases/latest"
UPDATE_CHECK_TIMEOUT_SECONDS = 2


# ============================================================================
# PYTHON VERSION CHECK
# ============================================================================


if BASE_CONSTANTS.PYTHON_VERSION < (3, 10):
    print("[RecORDER] Python version < 3.10, correct behaviour is not guaranteed!")


# ============================================================================
# LOGGING
# ============================================================================


_verbose_logging = False

# obs.script_log() is only safe on the OBS UI thread. Anything logged from the
# file-move worker or the muxer thread is parked here and flushed by a timer.
_deferred_logs = deque()
_deferred_logs_lock = threading.Lock()


def _write_log_line(level: str, text: str) -> None:
    """Hand one already formatted line to OBS. UI thread only."""
    try:
        if level == "error":
            obs.script_log(obs.LOG_ERROR, text)
        elif level == "warning":
            obs.script_log(obs.LOG_WARNING, text)
        else:
            obs.script_log(obs.LOG_INFO, text)
    except Exception:
        # Running outside OBS (or too early) - stdout is better than nothing.
        print(text)


def _flush_deferred_logs() -> None:
    """Drain the off-thread log backlog. A plain function: OBS timers need one."""
    while True:
        with _deferred_logs_lock:
            if not _deferred_logs:
                return
            level, text = _deferred_logs.popleft()
        _write_log_line(level, text)


def log(level: str, message: str) -> None:
    """Log through OBS' script log. `level` is one of debug/info/warning/error."""
    if level == "debug" and not _verbose_logging:
        return

    text = "[RecORDER] " + str(message)

    if threading.current_thread() is threading.main_thread():
        _write_log_line(level, text)
        return

    with _deferred_logs_lock:
        _deferred_logs.append((level, text))


def log_exception(where: str) -> None:
    log("error", "Unhandled error in %s:\n%s" % (where, traceback.format_exc()))


def guarded(func):
    """Wrap an OBS-invoked entry point so an exception is logged, never propagated."""

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            log_exception(getattr(func, "__name__", "callback"))
            return None

    wrapper.__name__ = getattr(func, "__name__", "wrapper")
    wrapper.__doc__ = getattr(func, "__doc__", None)
    return wrapper


# ============================================================================
# NAME SANITIZATION
# ============================================================================


# Characters Windows forbids in a path component.
_ILLEGAL_PATH_CHARS = re.compile(r'[<>:"/\\|?*]')

# Control characters become spaces so words do not get glued together.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

_RESERVED_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % i for i in range(1, 10)]
    + ["LPT%d" % i for i in range(1, 10)]
)

# Titles that are technically valid but useless as a folder name.
_JUNK_TITLES = frozenset(["", "untitled", "program manager", "default ime", "msctfime ui"])


def sanitize_folder_name(name: Optional[str], max_length: int = MAX_FOLDER_NAME_LENGTH) -> str:
    """
    Turn an arbitrary window title into something safe to use as a folder name.

    Unicode letters are kept (non-Latin game titles must survive); only what
    Windows actually rejects is removed.
    """
    if not name:
        return ""

    cleaned = _CONTROL_CHARS.sub(" ", str(name))
    cleaned = _ILLEGAL_PATH_CHARS.sub("", cleaned)

    # Collapse all runs of whitespace (also strips leading/trailing whitespace).
    cleaned = " ".join(cleaned.split())

    # Windows silently drops trailing dots and spaces from folder names.
    cleaned = cleaned.rstrip(". ")

    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(". ")

    if not cleaned:
        return ""

    # "NUL", "COM1", but also "NUL.txt" are reserved device names on Windows.
    stem = cleaned.split(".")[0].strip().upper()
    if stem in _RESERVED_DEVICE_NAMES:
        cleaned = "_" + cleaned

    return cleaned


def executable_stem(executable: Optional[str]) -> str:
    """'C:\\Games\\eldenring.exe' -> 'eldenring'."""
    if not executable:
        return ""
    base = os.path.basename(str(executable).replace("\\", "/"))
    return os.path.splitext(base)[0]


def _is_usable_name(name: str) -> bool:
    return bool(name) and name.strip().lower() not in _JUNK_TITLES


def derive_folder_name(
    title: Optional[str],
    executable: Optional[str],
    fallback_name: Optional[str],
    prefer_executable: bool = False,
) -> str:
    """
    Pick the folder name for a media file.

    Order: preferred candidate -> the other candidate -> user fallback -> built-in default.
    """
    candidates = [title, executable_stem(executable)]
    if prefer_executable:
        candidates.reverse()

    for candidate in candidates:
        sanitized = sanitize_folder_name(candidate)
        if _is_usable_name(sanitized):
            return sanitized

    fallback = sanitize_folder_name(fallback_name)
    if fallback:
        return fallback
    return DEFAULT_FALLBACK_FOLDER_NAME


# ============================================================================
# DATA CLASSES
# ============================================================================


class RecORDERProperties:
    """
    User-configurable settings that control the customizable script behaviour.

    This class represents what the user can personalize in OBS settings.
    """

    def __init__(
        self,
        game_title_prefix: bool = False,
        enable_replay_organization: bool = True,
        enable_screenshot_organization: bool = True,
        replay_folder_name: str = DEFAULT_REPLAY_FOLDER_NAME,
        screenshot_folder_name: str = DEFAULT_SCREENSHOT_FOLDER_NAME,
        fallback_window_title: str = DEFAULT_FALLBACK_FOLDER_NAME,
        selected_source_uuid: str = "",
        selected_organization_mode: str = AVAILABLE_ORGANIZATION_MODES.BASIC,
        hook_memory_seconds: int = DEFAULT_HOOK_MEMORY_SECONDS,
        folder_name_source: str = FOLDER_NAME_SOURCES.WINDOW_TITLE,
    ):
        self.fallback_window_title: str = (
            sanitize_folder_name(fallback_window_title) or DEFAULT_FALLBACK_FOLDER_NAME
        )
        self.replay_folder_name: str = (
            sanitize_folder_name(replay_folder_name) or DEFAULT_REPLAY_FOLDER_NAME
        )
        self.screenshot_folder_name: str = (
            sanitize_folder_name(screenshot_folder_name) or DEFAULT_SCREENSHOT_FOLDER_NAME
        )
        self.selected_source_uuid: str = selected_source_uuid or ""
        self.selected_organization_mode: str = (
            selected_organization_mode or AVAILABLE_ORGANIZATION_MODES.BASIC
        )

        self.game_title_prefix: bool = bool(game_title_prefix)
        self.enable_replay_organization: bool = bool(enable_replay_organization)
        self.enable_screenshot_organization: bool = bool(enable_screenshot_organization)

        try:
            self.hook_memory_seconds: int = max(0, int(hook_memory_seconds))
        except (TypeError, ValueError):
            self.hook_memory_seconds = DEFAULT_HOOK_MEMORY_SECONDS

        self.folder_name_source: str = folder_name_source or FOLDER_NAME_SOURCES.WINDOW_TITLE

    @property
    def prefer_executable(self) -> bool:
        return self.folder_name_source == FOLDER_NAME_SOURCES.EXECUTABLE


class HookInfo:
    """
    Everything we remember about the game window a hook-able source is (or was) on.

    Updated by the `hooked`/`unhooked` signals and by every successful live query.
    Timestamps use time.monotonic() so a clock change cannot confuse the grace window.
    """

    def __init__(self):
        self.title: Optional[str] = None
        self.executable: Optional[str] = None
        self.hooked_at: Optional[float] = None
        self.unhooked_at: Optional[float] = None

    def set_hooked(self, title: Optional[str], executable: Optional[str]) -> None:
        if title:
            self.title = title
        if executable:
            self.executable = executable
        self.hooked_at = time.monotonic()
        self.unhooked_at = None

    def set_unhooked(self) -> None:
        self.unhooked_at = time.monotonic()

    def reset(self) -> None:
        """Forget everything. Used when the sources we knew about are destroyed."""
        self.title = None
        self.executable = None
        self.hooked_at = None
        self.unhooked_at = None

    def has_memory(self) -> bool:
        return bool(self.title or self.executable)

    def age_seconds(self) -> Optional[float]:
        """Seconds since the hook was lost, or since it was last confirmed."""
        reference = self.unhooked_at if self.unhooked_at is not None else self.hooked_at
        if reference is None:
            return None
        return max(0.0, time.monotonic() - reference)


class RecordingState:
    """Tracks the current recording session, including auto-split file paths."""

    def __init__(self):
        self.last_file_path: Optional[str] = None

    def reset(self) -> None:
        self.last_file_path = None


class ReplayState:
    """Manages replay buffer state, keeping it separate from normal recording."""

    def __init__(self):
        self.last_file_path: Optional[str] = None

    def reset(self) -> None:
        self.last_file_path = None


# ============================================================================
# SCRIPT CONFIGURATION MANAGEMENT
# ============================================================================


class ConfigManager:
    def __init__(self, config_path: str):
        self.__config_path: str = config_path
        self.config: dict = self.__loadConfig()

    def __loadConfig(self) -> dict:
        """Load script configuration from JSON file, return empty dictionary if it doesn't exist."""
        if os.path.exists(self.__config_path):
            try:
                with open(self.__config_path, "r", encoding="utf-8") as config_file:
                    loaded = json.load(config_file)
                    if isinstance(loaded, dict):
                        return loaded
            except Exception as e:
                log("warning", "[Config Manager] Could not read the config file: %s" % e)
        return dict()

    def __saveConfig(self) -> None:
        """Write current configuration back to JSON file."""
        try:
            directory = os.path.dirname(self.__config_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.__config_path, "w", encoding="utf-8") as config_file:
                json.dump(self.config, config_file, indent=2, sort_keys=True)
        except Exception as e:
            log("warning", "[Config Manager] Could not write the config file: %s" % e)

    def saveSourceForScene(self, scene_collection: str, scene_name: str, source_uuid: str) -> None:
        """Save mapping: scene_collection -> scene_name -> source_uuid."""
        if scene_collection not in self.config or not isinstance(
            self.config.get(scene_collection), dict
        ):
            self.config[scene_collection] = {}
        self.config[scene_collection][scene_name] = source_uuid
        self.__saveConfig()

    def getSourceForScene(self, scene_collection: str, scene_name: str) -> Optional[str]:
        """Retrieve the saved source uuid for a scene, None if there is no mapping."""
        collection = self.config.get(scene_collection)
        if not isinstance(collection, dict):
            return None
        return collection.get(scene_name)


# ============================================================================
# SCENE WALKING / SOURCE DISCOVERY
# ============================================================================


def is_hook_capable(source) -> bool:
    """True for sources that can report a hooked window (game/window capture)."""
    if source is None:
        return False
    return obs.obs_source_get_id(source) in SUPPORTED_SOURCE_TYPES.ALL


def query_hooked_state(source) -> Optional[dict]:
    """
    Ask a source, right now, whether it is hooked and to what.

    Returns {'hooked': bool, 'title': str|None, 'executable': str|None} or None
    when the source does not implement the `get_hooked` procedure.
    """
    calldata = None
    try:
        calldata = obs.calldata_create()
        procedure_handler = obs.obs_source_get_proc_handler(source)
        called = obs.proc_handler_call(
            procedure_handler, OBS_EVENT_NAMES.GET_HOOKED_PROCEDURE_NAME, calldata
        )

        if called is False:
            log(
                "debug",
                "Source '%s' has no '%s' procedure"
                % (obs.obs_source_get_name(source), OBS_EVENT_NAMES.GET_HOOKED_PROCEDURE_NAME),
            )
            return None

        title = obs.calldata_string(calldata, OBS_EVENT_NAMES.TITLE_CALLDATA_NAME_WINDOWS)
        if not title:
            title = obs.calldata_string(calldata, OBS_EVENT_NAMES.TITLE_CALLDATA_NAME_XCOMPOSITE)

        return {
            "hooked": bool(obs.calldata_bool(calldata, OBS_EVENT_NAMES.HOOKED_SIGNAL_NAME)),
            "title": title,
            "executable": obs.calldata_string(calldata, OBS_EVENT_NAMES.EXECUTABLE_CALLDATA_NAME),
        }

    except Exception as e:
        log("warning", "Hook query failed: %s" % e)
        return None

    finally:
        if calldata is not None:
            obs.calldata_destroy(calldata)


def enumerate_hookable_sources() -> list:
    """
    Walk the current scene (including groups and nested scenes) and return every
    hook-capable source as {'name', 'uuid', 'id', 'hooked', 'index'}.

    Visibility is deliberately NOT used as a filter - a hidden game capture is
    still the source that knows the game name.
    """
    found = []
    visited_uuids = set()

    scene_source = obs.obs_frontend_get_current_scene()
    if scene_source is None:
        return found

    try:
        root_scene = obs.obs_scene_from_source(scene_source)
        if root_scene is None:
            return found
        # Seed the visited set so a scene nested inside itself cannot recurse.
        root_uuid = obs.obs_source_get_uuid(scene_source)
        if root_uuid:
            visited_uuids.add(root_uuid)
        _walk_scene(root_scene, found, visited_uuids, depth=0)
    except Exception as e:
        log("warning", "Could not enumerate the current scene: %s" % e)
    finally:
        obs.obs_source_release(scene_source)

    return found


def _walk_scene(scene, found: list, visited_uuids: set, depth: int) -> None:
    """Recursive helper for enumerate_hookable_sources()."""
    if scene is None or depth > 8:
        return

    scene_items = None
    try:
        scene_items = obs.obs_scene_enum_items(scene)
        if not scene_items:
            return

        for item in scene_items:
            source = obs.obs_sceneitem_get_source(item)
            if source is None:
                continue

            uuid = obs.obs_source_get_uuid(source)
            if uuid in visited_uuids:
                continue
            visited_uuids.add(uuid)

            if obs.obs_sceneitem_is_group(item):
                _walk_scene(
                    obs.obs_sceneitem_group_get_scene(item), found, visited_uuids, depth + 1
                )
                continue

            nested_scene = obs.obs_scene_from_source(source)
            if nested_scene is not None:
                _walk_scene(nested_scene, found, visited_uuids, depth + 1)
                continue

            if is_hook_capable(source):
                state = query_hooked_state(source)
                found.append(
                    {
                        "name": obs.obs_source_get_name(source),
                        "uuid": uuid,
                        "id": obs.obs_source_get_id(source),
                        "hooked": bool(state and state.get("hooked")),
                        "index": len(found),
                    }
                )

    except Exception as e:
        log("warning", "Error while walking a scene: %s" % e)
    finally:
        if scene_items is not None:
            obs.sceneitem_list_release(scene_items)


def auto_detect_source_uuid() -> Optional[str]:
    """Pick the best hook-capable source in the current scene: hooked first, then game capture."""
    candidates = enumerate_hookable_sources()
    if not candidates:
        return None

    def rank(candidate):
        return (
            0 if candidate["hooked"] else 1,
            0 if candidate["id"] == SUPPORTED_SOURCE_TYPES.GAME_CAPTURE else 1,
            candidate["index"],
        )

    best = sorted(candidates, key=rank)[0]
    log("debug", "Auto-detect picked source '%s' (%s)" % (best["name"], best["id"]))
    return best["uuid"]


# ============================================================================
# HOOK SIGNAL MANAGEMENT
# ============================================================================


class HookManager:
    """
    Owns the connection to the hook-able source: `hooked`/`unhooked` signals and
    the live `get_hooked` query.

    The signal callbacks are plain closures created once and reused, because the
    obspython signal wrappers silently ignore bound methods.
    """

    def __init__(self, properties: RecORDERProperties, hook_info: HookInfo):
        self.properties: RecORDERProperties = properties
        self.hook_info: HookInfo = hook_info
        self.source_uuid: Optional[str] = None

        def on_hooked(calldata) -> None:
            try:
                title = obs.calldata_string(calldata, OBS_EVENT_NAMES.TITLE_CALLDATA_NAME_WINDOWS)
                if not title:
                    title = obs.calldata_string(
                        calldata, OBS_EVENT_NAMES.TITLE_CALLDATA_NAME_XCOMPOSITE
                    )
                executable = obs.calldata_string(
                    calldata, OBS_EVENT_NAMES.EXECUTABLE_CALLDATA_NAME
                )
                self.hook_info.set_hooked(title, executable)
                log("debug", "Hooked: title=%r executable=%r" % (title, executable))
            except Exception:
                log_exception("hooked signal callback")

        def on_unhooked(calldata) -> None:
            try:
                self.hook_info.set_unhooked()
                log("debug", "Unhooked (remembering %r for now)" % (self.hook_info.title,))
            except Exception:
                log_exception("unhooked signal callback")

        self._on_hooked = on_hooked
        self._on_unhooked = on_unhooked

    # -- public API ---------------------------------------------------------

    def connect(self) -> bool:
        """Connect to the configured source, or auto-detect one. Idempotent."""
        target_uuid = self.__resolveTargetUuid()

        if target_uuid is None:
            if self.source_uuid is not None:
                self.disconnect()
            log("debug", "No hook-capable source available in the current scene")
            return False

        if self.source_uuid == target_uuid and self.__sourceExists(target_uuid):
            return True

        self.disconnect()
        return self.__attach(target_uuid)

    def ensure_connected(self) -> bool:
        """Reconnect if we never connected, if the source vanished, or if the selection changed."""
        if self.source_uuid is not None:
            if self.__sourceExists(self.source_uuid):
                configured = (self.properties.selected_source_uuid or "").strip()
                if not configured or configured == self.source_uuid:
                    return True
            else:
                log("debug", "Monitored source disappeared - re-discovering")
                self.source_uuid = None

        return self.connect()

    def disconnect(self) -> None:
        """Detach the signal callbacks. Safe to call when nothing is connected."""
        if self.source_uuid is None:
            return

        source = obs.obs_get_source_by_uuid(self.source_uuid)
        if source is None:
            log("debug", "Source already gone, nothing to disconnect")
        else:
            try:
                handler = obs.obs_source_get_signal_handler(source)
                obs.signal_handler_disconnect(
                    handler, OBS_EVENT_NAMES.HOOKED_SIGNAL_NAME, self._on_hooked
                )
                obs.signal_handler_disconnect(
                    handler, OBS_EVENT_NAMES.UNHOOKED_SIGNAL_NAME, self._on_unhooked
                )
            except Exception as e:
                log("warning", "Failed to disconnect hook signals: %s" % e)
            finally:
                obs.obs_source_release(source)

        self.source_uuid = None

    def query_live(self) -> Optional[dict]:
        """Return the live hook state of the monitored source, or None."""
        if self.source_uuid is None:
            return None

        source = obs.obs_get_source_by_uuid(self.source_uuid)
        if source is None:
            self.source_uuid = None
            return None

        try:
            return query_hooked_state(source)
        finally:
            obs.obs_source_release(source)

    # -- internals ----------------------------------------------------------

    def __sourceExists(self, uuid: str) -> bool:
        source = obs.obs_get_source_by_uuid(uuid)
        if source is None:
            return False
        obs.obs_source_release(source)
        return True

    def __resolveTargetUuid(self) -> Optional[str]:
        configured = (self.properties.selected_source_uuid or "").strip()

        if configured:
            source = obs.obs_get_source_by_uuid(configured)
            if source is None:
                log("warning", "Selected source no longer exists - falling back to auto-detect")
            else:
                try:
                    if is_hook_capable(source):
                        return configured
                    log(
                        "warning",
                        "Selected source '%s' is a '%s' which cannot report hooked windows; "
                        "pick a Game Capture or Window Capture instead."
                        % (obs.obs_source_get_name(source), obs.obs_source_get_id(source)),
                    )
                finally:
                    obs.obs_source_release(source)

        return auto_detect_source_uuid()

    def __attach(self, uuid: str) -> bool:
        source = obs.obs_get_source_by_uuid(uuid)
        if source is None:
            return False

        try:
            handler = obs.obs_source_get_signal_handler(source)
            obs.signal_handler_connect(handler, OBS_EVENT_NAMES.HOOKED_SIGNAL_NAME, self._on_hooked)
            obs.signal_handler_connect(
                handler, OBS_EVENT_NAMES.UNHOOKED_SIGNAL_NAME, self._on_unhooked
            )
            self.source_uuid = uuid

            log(
                "info",
                "Monitoring source '%s' (%s)"
                % (obs.obs_source_get_name(source), obs.obs_source_get_id(source)),
            )

            # Prime the memory in case the game was already hooked before we loaded.
            state = query_hooked_state(source)
            if state and state.get("hooked"):
                self.hook_info.set_hooked(state.get("title"), state.get("executable"))

            return True

        except Exception as e:
            log("error", "Could not connect to hook signals: %s" % e)
            return False

        finally:
            obs.obs_source_release(source)


# ============================================================================
# HOOK-ABLE WINDOW TITLE MANAGEMENT
# ============================================================================


class TitleResolver:
    """
    Decides which folder name a media file belongs in.

    Tiers, in order: a live `get_hooked` query -> the remembered hook (inside the
    grace window) -> the user's fallback name.
    """

    def __init__(
        self, properties: RecORDERProperties, hook_info: HookInfo, hook_manager: HookManager
    ):
        self.properties: RecORDERProperties = properties
        self.hook_info: HookInfo = hook_info
        self.hook_manager: HookManager = hook_manager

    def resolve(self) -> tuple:
        """Return (folder_name, tier)."""
        title = None
        executable = None
        tier = RESOLUTION_TIERS.FALLBACK

        state = self.hook_manager.query_live()
        if state is not None and state.get("hooked"):
            title = state.get("title")
            executable = state.get("executable")
            self.hook_info.set_hooked(title, executable)
            tier = RESOLUTION_TIERS.LIVE
        else:
            grace = self.properties.hook_memory_seconds
            age = self.hook_info.age_seconds()
            if grace > 0 and self.hook_info.has_memory() and age is not None and age <= grace:
                title = self.hook_info.title
                executable = self.hook_info.executable
                tier = RESOLUTION_TIERS.MEMORY

        folder_name = derive_folder_name(
            title,
            executable,
            self.properties.fallback_window_title,
            prefer_executable=self.properties.prefer_executable,
        )

        log("info", "Folder name '%s' (from %s)" % (folder_name, tier))
        return folder_name, tier


# ============================================================================
# FILE MOVING (background worker)
# ============================================================================


class MoveJob:
    def __init__(
        self,
        source_path: str,
        destination_dir: str,
        prefix: str = "",
        tier: str = "",
        wait_for_remux: bool = False,
    ):
        self.source_path: str = source_path
        self.destination_dir: str = destination_dir
        self.prefix: str = prefix or ""
        self.tier: str = tier
        self.wait_for_remux: bool = wait_for_remux


def _unique_destination(destination_dir: str, filename: str) -> str:
    """Never overwrite: x.mkv -> 'x (1).mkv' -> 'x (2).mkv' ..."""
    stem, extension = os.path.splitext(filename)
    candidate = os.path.join(destination_dir, filename)

    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(destination_dir, "%s (%d)%s" % (stem, counter, extension))
        counter += 1

    return candidate


def _wait_until_size_is_stable(path: str) -> None:
    """Wait until the file size stops changing - OBS may still be flushing it."""
    previous_size = -1
    for _ in range(SIZE_STABLE_MAX_SAMPLES):
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size == previous_size:
            return
        previous_size = size
        time.sleep(SIZE_STABLE_POLL_SECONDS)

    log("debug", "File size never settled for %s, moving anyway" % path)


class FileMoveWorker:
    """
    One long-lived worker thread draining a queue of MoveJobs.

    A single worker means moves are serialized, which is exactly what we want:
    no two jobs can race for the same destination name.
    """

    _SENTINEL = object()

    def __init__(self):
        self._queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._pending = 0
        self._condition = threading.Condition()
        self._stopping = False

    def submit(self, job: MoveJob) -> None:
        if self._stopping:
            log("warning", "Worker is shutting down, dropping job for %s" % job.source_path)
            return

        with self._condition:
            self._pending += 1

        self.__ensureRunning()
        self._queue.put(job)

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until every queued job has been handled. Mostly for tests/shutdown."""
        with self._condition:
            return self._condition.wait_for(lambda: self._pending == 0, timeout)

    def shutdown(self, timeout: float = WORKER_JOIN_TIMEOUT_SECONDS) -> None:
        """Stop the worker, giving in-flight moves a bounded chance to finish."""
        self._stopping = True

        thread = self._thread
        if thread is None:
            return

        self._queue.put(self._SENTINEL)
        thread.join(timeout)

        if thread.is_alive():
            log("warning", "A file move was still running after %.1fs - it may be incomplete" % timeout)
        self._thread = None

    def __ensureRunning(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.__run, name="RecORDER-file-mover", daemon=True
        )
        self._thread.start()

    def __run(self) -> None:
        while True:
            job = self._queue.get()
            if job is self._SENTINEL:
                return

            try:
                self.__process(job)
            except Exception:
                log_exception("file move worker")
            finally:
                with self._condition:
                    self._pending -= 1
                    self._condition.notify_all()

    def __process(self, job: MoveJob) -> None:
        if not os.path.exists(job.source_path):
            log("warning", "File disappeared before it could be organized: %s" % job.source_path)
            return

        _wait_until_size_is_stable(job.source_path)

        paths = [job.source_path]

        if job.wait_for_remux:
            remuxed = self.__waitForRemux(job.source_path)
            if remuxed:
                paths.append(remuxed)

        for path in paths:
            if not os.path.exists(path):
                continue
            try:
                destination = self.__move(path, job)
                log(
                    "info",
                    "Organized %s -> %s (folder from %s)" % (path, destination, job.tier),
                )
            except Exception as e:
                log("error", "Could not move %s: %s" % (path, e))

    def __move(self, path: str, job: MoveJob) -> str:
        os.makedirs(job.destination_dir, exist_ok=True)

        basename = os.path.basename(path)
        filename = "%s - %s" % (job.prefix, basename) if job.prefix else basename

        last_error = None
        for attempt in range(1, MOVE_MAX_ATTEMPTS + 1):
            destination = _unique_destination(job.destination_dir, filename)
            try:
                shutil.move(path, destination)
                return destination
            except (PermissionError, OSError) as error:
                last_error = error
                if attempt == MOVE_MAX_ATTEMPTS:
                    break
                delay = min(
                    MOVE_RETRY_BASE_SECONDS * (2 ** (attempt - 1)), MOVE_RETRY_MAX_SECONDS
                )
                log(
                    "debug",
                    "Move attempt %d/%d for %s failed (%s), retrying in %.2fs"
                    % (attempt, MOVE_MAX_ATTEMPTS, path, error, delay),
                )
                time.sleep(delay)

        raise last_error if last_error is not None else RuntimeError("move failed")

    def __waitForRemux(self, path: str) -> Optional[str]:
        """
        Best effort support for OBS' 'Automatically remux to mp4'.

        Waits (bounded) for '<same name>.mp4' to appear next to the recording and
        settle, so we can move the original and the remuxed file together.
        """
        stem, extension = os.path.splitext(path)
        if extension.lower() in (".mp4", ".mov"):
            return None

        remuxed = stem + ".mp4"
        deadline = time.monotonic() + REMUX_TIMEOUT_SECONDS

        log("debug", "Waiting for auto-remux of %s" % path)

        while time.monotonic() < deadline:
            if self._stopping:
                log("debug", "Shutting down, stopped waiting for the remux of %s" % path)
                return None
            if os.path.exists(remuxed):
                _wait_until_size_is_stable(remuxed)
                log("info", "Auto-remux finished: %s" % remuxed)
                return remuxed
            if not os.path.exists(path):
                # OBS deletes the original once remuxing succeeds.
                if os.path.exists(remuxed):
                    continue
                log("debug", "Original recording vanished while waiting for the remux")
                return None
            time.sleep(REMUX_POLL_SECONDS)

        log("warning", "Auto-remux of %s did not finish in time - moving the original only" % path)
        return None


# ---------------------------------------------------------------------------
# One worker for the whole script, not one per core.
#
# Reconfiguring the script (script_update) builds a brand new orchestrator; if
# the worker belonged to it, the rebuild would have to join the old thread on
# the UI thread and freeze OBS for as long as a move takes. MoveJobs are
# self-contained - every path decision is made at submit time on the UI thread -
# so a single shared worker can outlive any number of rebuilds and is only
# joined in script_unload.
# ---------------------------------------------------------------------------

_file_move_worker: Optional[FileMoveWorker] = None


def get_file_move_worker() -> FileMoveWorker:
    """Return the shared worker, creating it on first use."""
    global _file_move_worker
    if _file_move_worker is None:
        _file_move_worker = FileMoveWorker()
    return _file_move_worker


def shutdown_file_move_worker(timeout: Optional[float] = None) -> None:
    """Drain and stop the shared worker. Only script_unload should call this."""
    global _file_move_worker
    worker = _file_move_worker
    _file_move_worker = None
    if worker is not None:
        worker.shutdown(WORKER_JOIN_TIMEOUT_SECONDS if timeout is None else timeout)


# ============================================================================
# FILE ORGANIZATION
# ============================================================================


def is_auto_remux_enabled() -> bool:
    """Check the active OBS profile for 'Automatically remux to mp4'."""
    try:
        config = obs.obs_frontend_get_profile_config()
        if config is None:
            return False
        return bool(obs.config_get_bool(config, "Video", "AutoRemux"))
    except Exception as e:
        log("debug", "Could not read the AutoRemux profile setting: %s" % e)
        return False


class MediaFileOrganizer:
    def __init__(
        self,
        title_resolver: TitleResolver,
        mover: FileMoveWorker,
        organization_mode: str = AVAILABLE_ORGANIZATION_MODES.BASIC,
        replay_folder_name: str = DEFAULT_REPLAY_FOLDER_NAME,
        screenshot_folder_name: str = DEFAULT_SCREENSHOT_FOLDER_NAME,
        title_as_prefix: bool = False,
    ):
        self.title_resolver: TitleResolver = title_resolver
        self.mover: FileMoveWorker = mover
        self.title_as_prefix: bool = title_as_prefix
        self.organization_mode: str = organization_mode
        self.replay_folder_name: str = replay_folder_name
        self.screenshot_folder_name: str = screenshot_folder_name

        # Cached on the UI thread: recording splits are reported on the muxer
        # thread, which has no business calling obs_frontend_* functions.
        self.auto_remux_enabled: bool = False

    def refreshAutoRemuxFlag(self) -> None:
        """Read the profile's AutoRemux setting. Call from the OBS UI thread only."""
        self.auto_remux_enabled = is_auto_remux_enabled()

    def processRecording(self, file_path: str) -> None:
        self.__organize(
            file_path,
            SUPPORTED_MEDIAFILE_TYPES.RECORDING,
            wait_for_remux=self.auto_remux_enabled,
        )

    def processReplay(self, file_path: str) -> None:
        self.__organize(
            file_path, SUPPORTED_MEDIAFILE_TYPES.REPLAY, folder_name=self.replay_folder_name
        )

    def processScreenshot(self, file_path: str) -> None:
        self.__organize(
            file_path,
            SUPPORTED_MEDIAFILE_TYPES.SCREENSHOT,
            folder_name=self.screenshot_folder_name,
        )

    def __organize(
        self,
        file_path: str,
        media_type: str,
        folder_name: Optional[str] = None,
        wait_for_remux: bool = False,
    ) -> None:
        if not file_path:
            log("warning", "Nothing to organize (no file path for a %s)" % media_type)
            return

        game_title, tier = self.title_resolver.resolve()
        destination_dir = self.calculateDestinationDir(
            file_path, game_title, media_type, folder_name
        )

        self.mover.submit(
            MoveJob(
                source_path=file_path,
                destination_dir=destination_dir,
                prefix=game_title if self.title_as_prefix else "",
                tier=tier,
                wait_for_remux=wait_for_remux,
            )
        )

    def calculateDestinationDir(
        self, file_path: str, game_title: str, media_type: str, folder_name: Optional[str]
    ) -> str:
        """<recording dir>/<Game>/[<replay|screenshot folder>]/[<yy-mm-dd>]"""
        directory = os.path.dirname(file_path)
        parts = [directory, game_title]

        if media_type != SUPPORTED_MEDIAFILE_TYPES.RECORDING and folder_name:
            parts.append(folder_name)

        if self.organization_mode == AVAILABLE_ORGANIZATION_MODES.DATE_BASED:
            parts.append(self.__creationDate(file_path))
        elif self.organization_mode != AVAILABLE_ORGANIZATION_MODES.BASIC:
            log(
                "warning",
                "Unknown organization mode %r - using the basic layout"
                % (self.organization_mode,),
            )

        return os.path.join(*parts)

    def __creationDate(self, file_path: str) -> str:
        try:
            created = os.path.getctime(file_path)
        except OSError:
            created = time.time()
        return datetime.fromtimestamp(created).strftime("%y-%m-%d")


# ============================================================================
# RECORDING MANAGEMENT
# ============================================================================


class RecordingManager:
    """Handles normal recordings, including the splits OBS makes on its own."""

    def __init__(self, state: RecordingState, organizer: MediaFileOrganizer):
        self.state: RecordingState = state
        self.organizer: MediaFileOrganizer = organizer
        self._processed_paths = set()
        # file_changed arrives on the muxer thread while stop() runs on the UI
        # thread, so the "have I already handled this?" test-and-set is guarded.
        self._processed_lock = threading.Lock()
        self._connected: bool = False

        # A plain closure: obspython refuses bound methods for signals.
        def on_file_changed(calldata) -> None:
            try:
                self.__onFileChange(calldata)
            except Exception:
                log_exception("file_changed signal callback")

        self._callback = on_file_changed

    def start(self) -> None:
        with self._processed_lock:
            self._processed_paths.clear()
        self.state.last_file_path = obs.obs_frontend_get_last_recording()
        self.connectSplitMonitoring()
        log("info", "Recording started: %s" % self.state.last_file_path)

    def adopt(self, file_path: str) -> None:
        """Take over an in-progress recording after the script was reconfigured."""
        self.state.last_file_path = file_path
        self.connectSplitMonitoring()
        log("debug", "Took over an in-progress recording: %s" % file_path)

    def stop(self) -> None:
        file_path = self.state.last_file_path
        self.disconnectSplitMonitoring()

        if file_path:
            self.__process(file_path)
        else:
            log("warning", "Recording stopped but no file path was known")

        self.state.reset()

    def connectSplitMonitoring(self) -> None:
        """Connect to `file_changed` on the recording output (fires on splits)."""
        self.disconnectSplitMonitoring()

        output = obs.obs_frontend_get_recording_output()
        if output is None:
            log("warning", "No recording output available - split monitoring is off")
            return

        try:
            handler = obs.obs_output_get_signal_handler(output)
            obs.signal_handler_connect(
                handler, OBS_EVENT_NAMES.FILE_CHANGED_SIGNAL_NAME, self._callback
            )
            self._connected = True
            log("debug", "Split monitoring enabled")
        except Exception as e:
            log("warning", "Failed to set up split monitoring: %s" % e)
        finally:
            obs.obs_output_release(output)

    def disconnectSplitMonitoring(self) -> None:
        if not self._connected:
            return

        output = obs.obs_frontend_get_recording_output()
        if output is None:
            self._connected = False
            return

        try:
            handler = obs.obs_output_get_signal_handler(output)
            obs.signal_handler_disconnect(
                handler, OBS_EVENT_NAMES.FILE_CHANGED_SIGNAL_NAME, self._callback
            )
        except Exception as e:
            log("warning", "Failed to disconnect split monitoring: %s" % e)
        finally:
            obs.obs_output_release(output)
            self._connected = False

    def __process(self, file_path: str) -> None:
        with self._processed_lock:
            if file_path in self._processed_paths:
                log("debug", "Already organized %s - skipping" % file_path)
                return
            self._processed_paths.add(file_path)
        self.organizer.processRecording(file_path)

    def __onFileChange(self, calldata) -> None:
        """
        Runs on the muxer thread, so it stays small: read the new path, remember
        it, and hand the finished file to the organizer.

        Nothing here may call an obs_frontend_* function - those are UI-thread
        only. If the signal did not carry a path there is nothing safe to do but
        say so and leave the state alone; the recording-stopped handler (which
        does run on the UI thread) still organizes the final file.
        """
        next_file = obs.calldata_string(calldata, OBS_EVENT_NAMES.NEXT_FILE_CALLDATA_NAME)
        if not next_file:
            log(
                "warning",
                "A 'file_changed' signal carried no '%s' - ignoring this split"
                % OBS_EVENT_NAMES.NEXT_FILE_CALLDATA_NAME,
            )
            return

        previous_file = self.state.last_file_path
        self.state.last_file_path = next_file

        if previous_file and previous_file != next_file:
            log("info", "Recording split - organizing %s" % previous_file)
            self.__process(previous_file)


# ============================================================================
# REPLAY BUFFER MANAGEMENT
# ============================================================================


class ReplayManager:
    """Replay buffer clips: nothing to do until the user actually saves one."""

    def __init__(self, state: ReplayState, organizer: MediaFileOrganizer):
        self.state: ReplayState = state
        self.organizer: MediaFileOrganizer = organizer

    def start(self) -> None:
        log("debug", "Replay Buffer started")

    def stop(self) -> None:
        self.state.reset()
        log("debug", "Replay Buffer stopped")

    def processSavedReplay(self) -> None:
        file_path = obs.obs_frontend_get_last_replay()
        self.state.last_file_path = file_path

        if file_path:
            log("info", "Replay saved: %s" % file_path)
            self.organizer.processReplay(file_path)
        else:
            log("warning", "Replay buffer saved but OBS reported no file")


# ============================================================================
# CENTRAL ORCHESTRATOR
# ============================================================================


class RecORDER:
    def __init__(
        self,
        properties: RecORDERProperties,
        config_manager: Optional[ConfigManager],
        hook_info: Optional[HookInfo] = None,
    ):
        self.properties: RecORDERProperties = properties
        self.config_manager: Optional[ConfigManager] = config_manager

        self.hook_info: HookInfo = hook_info if hook_info is not None else HookInfo()
        self.recording_state: RecordingState = RecordingState()
        self.replay_state: ReplayState = ReplayState()

        self.hook_manager: HookManager = HookManager(properties, self.hook_info)
        self.title_resolver: TitleResolver = TitleResolver(
            properties, self.hook_info, self.hook_manager
        )
        # Shared with every other core: a rebuild must not join a running move.
        self.mover: FileMoveWorker = get_file_move_worker()
        self.organizer: MediaFileOrganizer = MediaFileOrganizer(
            title_resolver=self.title_resolver,
            mover=self.mover,
            organization_mode=properties.selected_organization_mode,
            replay_folder_name=properties.replay_folder_name,
            screenshot_folder_name=properties.screenshot_folder_name,
            title_as_prefix=properties.game_title_prefix,
        )
        self.recording_manager: RecordingManager = RecordingManager(
            state=self.recording_state, organizer=self.organizer
        )
        self.replay_manager: ReplayManager = ReplayManager(
            state=self.replay_state, organizer=self.organizer
        )

        self.event_handlers: dict = self.__buildEventHandlers()

    # -- lifecycle ----------------------------------------------------------

    def startup(self) -> None:
        """Discover and connect to a hook-able source."""
        self.hook_manager.connect()

    def adoptFrom(self, previous: "RecORDER") -> None:
        """Carry over in-progress recording state when the script is reconfigured."""
        file_path = previous.recording_state.last_file_path
        if file_path:
            self.organizer.refreshAutoRemuxFlag()
            self.recording_manager.adopt(file_path)

    def detach(self) -> None:
        """Drop every signal connection and reset state. Never touches OBS outputs."""
        self.hook_manager.disconnect()
        self.recording_manager.disconnectSplitMonitoring()

    def shutdown(self) -> None:
        """
        Unload/reconfigure cleanup: detach every signal.

        The file-move worker is deliberately NOT joined here - it is shared with
        whatever core replaces this one, and joining it would block the UI
        thread on every settings change. script_unload drains it instead.
        """
        log("debug", "Shutting down")
        self.detach()

    # -- event plumbing -----------------------------------------------------

    def __buildEventHandlers(self) -> dict:
        handlers = {}

        def bind(event_name, handler):
            event = getattr(obs, event_name, None)
            if event is None:
                log("debug", "This OBS build has no %s" % event_name)
                return
            handlers[event] = handler

        bind("OBS_FRONTEND_EVENT_RECORDING_STARTED", self.__handleRecordingStart)
        bind("OBS_FRONTEND_EVENT_RECORDING_STOPPED", self.__handleRecordingStop)
        bind("OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGING", self.__handleSceneCollectionChanging)
        bind("OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGED", self.__handleRediscover)
        bind("OBS_FRONTEND_EVENT_SCENE_CHANGED", self.__handleSceneChange)
        bind("OBS_FRONTEND_EVENT_FINISHED_LOADING", self.__handleRediscover)

        if self.properties.enable_replay_organization:
            bind("OBS_FRONTEND_EVENT_REPLAY_BUFFER_STARTED", self.__handleReplayStart)
            bind("OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED", self.__handleReplaySave)
            bind("OBS_FRONTEND_EVENT_REPLAY_BUFFER_STOPPED", self.__handleReplayStop)

        if self.properties.enable_screenshot_organization:
            bind("OBS_FRONTEND_EVENT_SCREENSHOT_TAKEN", self.__handleScreenshot)

        return handlers

    def dispatchEvent(self, event: int) -> None:
        handler = self.event_handlers.get(event)
        if handler is None:
            return
        try:
            handler()
        except Exception:
            log_exception("event handler for event %s" % event)

    # -- event handlers -----------------------------------------------------

    def __handleRecordingStart(self) -> None:
        self.hook_manager.ensure_connected()
        self.organizer.refreshAutoRemuxFlag()
        self.recording_manager.start()

    def __handleRecordingStop(self) -> None:
        # Mirror the start handler: a capture source added (or hooked) during
        # the recording deserves one live discovery attempt before the final
        # file is named.
        self.hook_manager.ensure_connected()
        self.organizer.refreshAutoRemuxFlag()
        self.recording_manager.stop()

    def __handleReplayStart(self) -> None:
        self.hook_manager.ensure_connected()
        self.replay_manager.start()

    def __handleReplaySave(self) -> None:
        self.hook_manager.ensure_connected()
        self.replay_manager.processSavedReplay()

    def __handleReplayStop(self) -> None:
        self.replay_manager.stop()

    def __handleScreenshot(self) -> None:
        self.hook_manager.ensure_connected()

        screenshot_path = obs.obs_frontend_get_last_screenshot()
        if screenshot_path:
            log("info", "Screenshot taken: %s" % screenshot_path)
            self.organizer.processScreenshot(screenshot_path)
        else:
            log("warning", "Screenshot taken but OBS reported no file")

    def __handleSceneCollectionChanging(self) -> None:
        """
        Sources are about to be destroyed - let go of them before that happens.

        The remembered hook belongs to the collection that is going away, so it
        is dropped too: a clip saved right after the switch must not be filed
        under a game from the previous collection. Note that a plain scene
        change does NOT do this - switching to an overlay scene mid-game has to
        keep the memory intact.
        """
        log("debug", "Scene collection changing - detaching and forgetting the last hook")
        self.hook_manager.disconnect()
        self.hook_info.reset()

    def __handleRediscover(self) -> None:
        self.hook_manager.connect()

    def __handleSceneChange(self) -> None:
        """A scene change may mean a different capture source: use the saved mapping if any."""
        try:
            saved_uuid = self.__savedSourceForCurrentScene()
            if saved_uuid:
                self.properties.selected_source_uuid = saved_uuid
        except Exception as e:
            log("warning", "Could not look up the source for this scene: %s" % e)

        self.hook_manager.connect()

    def __savedSourceForCurrentScene(self) -> Optional[str]:
        if self.config_manager is None:
            return None

        collection_name, scene_name = get_current_scene_identity()
        if not collection_name or not scene_name:
            return None

        return self.config_manager.getSourceForScene(collection_name, scene_name)


core: Optional[RecORDER] = None
config_manager: Optional[ConfigManager] = None
script_settings = None

# Every property value the current core was built from. OBS calls script_update
# right after script_load and again on every unrelated dialog interaction, so
# without this the script would rebuild itself for nothing.
_core_settings_signature: Optional[tuple] = None

# (scene collection, scene) -> uuid we last persisted, so we only write on change.
_last_saved_selection: dict = {}


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def get_config_path() -> str:
    """Path of the script's own JSON config (scene -> source mappings)."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, "RecORDERConfig.json")


def get_current_scene_identity() -> tuple:
    """Return (scene collection name, current scene name); either may be None."""
    scene_source = None
    try:
        collection_name = obs.obs_frontend_get_current_scene_collection()
        scene_source = obs.obs_frontend_get_current_scene()
        scene_name = obs.obs_source_get_name(scene_source) if scene_source is not None else None
        return collection_name, scene_name
    except Exception as e:
        log("warning", "Could not read the current scene: %s" % e)
        return None, None
    finally:
        if scene_source is not None:
            obs.obs_source_release(scene_source)


def get_latest_release_tag() -> Optional[str]:
    try:
        with urlopen(UPDATE_CHECK_URL, timeout=UPDATE_CHECK_TIMEOUT_SECONDS) as response:
            if getattr(response, "status", 200) == 200:
                data = json.load(response)
                return data.get("tag_name")
    except Exception as e:
        log("warning", "Failed to check for updates: %s" % e)
    return None


def is_update_available(current_version: str, latest_tag: Optional[str]) -> bool:
    if not latest_tag:
        return False
    latest = latest_tag[1:] if latest_tag[:1] in ("v", "V") else latest_tag
    return latest.strip() != str(current_version).strip()


def save_config(source_uuid: str) -> None:
    """Remember which source the user picked for the current scene (only when it changed)."""
    if not source_uuid or config_manager is None:
        return

    collection_name, scene_name = get_current_scene_identity()
    if not collection_name or not scene_name:
        return

    key = (collection_name, scene_name)
    if _last_saved_selection.get(key) == source_uuid:
        return

    _last_saved_selection[key] = source_uuid
    config_manager.saveSourceForScene(collection_name, scene_name, source_uuid)
    log("debug", "Saved source mapping for scene '%s'" % scene_name)


def build_properties_from_settings(settings) -> RecORDERProperties:
    return RecORDERProperties(
        selected_source_uuid=obs.obs_data_get_string(settings, PROPERTY_NAMES.SOURCE_SELECTOR),
        selected_organization_mode=obs.obs_data_get_string(
            settings, PROPERTY_NAMES.ORGANIZATION_MODE
        ),
        game_title_prefix=obs.obs_data_get_bool(settings, PROPERTY_NAMES.TITLE_AS_PREFIX),
        enable_replay_organization=obs.obs_data_get_bool(
            settings, PROPERTY_NAMES.ENABLE_REPLAY_ORGANIZATION
        ),
        enable_screenshot_organization=obs.obs_data_get_bool(
            settings, PROPERTY_NAMES.ENABLE_SCREENSHOT_ORGANIZATION
        ),
        fallback_window_title=obs.obs_data_get_string(settings, PROPERTY_NAMES.FALLBACK_WINDOW_NAME),
        replay_folder_name=obs.obs_data_get_string(settings, PROPERTY_NAMES.REPLAY_FOLDER_NAME),
        screenshot_folder_name=obs.obs_data_get_string(
            settings, PROPERTY_NAMES.SCREENSHOT_FOLDER_NAME
        ),
        hook_memory_seconds=obs.obs_data_get_int(settings, PROPERTY_NAMES.HOOK_MEMORY_SECONDS),
        folder_name_source=obs.obs_data_get_string(settings, PROPERTY_NAMES.FOLDER_NAME_SOURCE),
    )


def settings_signature(settings) -> tuple:
    """Every property value that influences the built core, as a comparable tuple."""
    return (
        obs.obs_data_get_string(settings, PROPERTY_NAMES.SOURCE_SELECTOR),
        obs.obs_data_get_string(settings, PROPERTY_NAMES.ORGANIZATION_MODE),
        bool(obs.obs_data_get_bool(settings, PROPERTY_NAMES.TITLE_AS_PREFIX)),
        bool(obs.obs_data_get_bool(settings, PROPERTY_NAMES.ENABLE_REPLAY_ORGANIZATION)),
        bool(obs.obs_data_get_bool(settings, PROPERTY_NAMES.ENABLE_SCREENSHOT_ORGANIZATION)),
        obs.obs_data_get_string(settings, PROPERTY_NAMES.FALLBACK_WINDOW_NAME),
        obs.obs_data_get_string(settings, PROPERTY_NAMES.REPLAY_FOLDER_NAME),
        obs.obs_data_get_string(settings, PROPERTY_NAMES.SCREENSHOT_FOLDER_NAME),
        obs.obs_data_get_int(settings, PROPERTY_NAMES.HOOK_MEMORY_SECONDS),
        obs.obs_data_get_string(settings, PROPERTY_NAMES.FOLDER_NAME_SOURCE),
        bool(obs.obs_data_get_bool(settings, PROPERTY_NAMES.VERBOSE_LOGGING)),
    )


def rebuild_core(settings) -> None:
    """Build a fresh orchestrator, swap it in, then retire the old one."""
    global core, _verbose_logging, _core_settings_signature

    _core_settings_signature = settings_signature(settings)
    _verbose_logging = bool(obs.obs_data_get_bool(settings, PROPERTY_NAMES.VERBOSE_LOGGING))

    properties = build_properties_from_settings(settings)
    save_config(properties.selected_source_uuid)

    previous = core
    new_core = RecORDER(
        properties,
        config_manager,
        hook_info=previous.hook_info if previous is not None else None,
    )

    core = new_core

    if previous is not None:
        new_core.adoptFrom(previous)

    new_core.startup()

    if previous is not None:
        previous.shutdown()


@guarded
def frontend_event_callback(event):
    if core is not None:
        core.dispatchEvent(event)


# ============================================================================
# METHODS TO POPULATE PROPERTY LISTS
# ============================================================================


def populate_source_selector(source_selector) -> None:
    """Fill the combo with every hook-able source in the current scene."""
    obs.obs_property_list_add_string(source_selector, "(Auto-detect)", "")

    selected_uuid = ""
    if script_settings is not None:
        try:
            selected_uuid = obs.obs_data_get_string(script_settings, PROPERTY_NAMES.SOURCE_SELECTOR)
        except Exception:
            selected_uuid = ""

    try:
        sources = enumerate_hookable_sources()
    except Exception as e:
        log("error", "Failed to populate the source selector: %s" % e)
        return

    known_uuids = set()
    for source in sources:
        obs.obs_property_list_add_string(source_selector, source["name"], source["uuid"])
        known_uuids.add(source["uuid"])

    # Keep the stored selection visible even when the source is not in this
    # scene, otherwise the combo silently rebinds to the first entry.
    if selected_uuid and selected_uuid not in known_uuids:
        obs.obs_property_list_add_string(
            source_selector, "%s (unavailable)" % selected_uuid[:8], selected_uuid
        )


def populate_organization_mode(organization_mode) -> None:
    obs.obs_property_list_add_string(organization_mode, "Basic", AVAILABLE_ORGANIZATION_MODES.BASIC)
    obs.obs_property_list_add_string(
        organization_mode, "Group by Date", AVAILABLE_ORGANIZATION_MODES.DATE_BASED
    )


def populate_folder_name_source(folder_name_source) -> None:
    obs.obs_property_list_add_string(
        folder_name_source, "Window title", FOLDER_NAME_SOURCES.WINDOW_TITLE
    )
    obs.obs_property_list_add_string(
        folder_name_source, "Executable name", FOLDER_NAME_SOURCES.EXECUTABLE
    )


# ============================================================================
# SETUPS FOR PROPERTIES GROUPS
# ============================================================================


def setup_customization(group_obj) -> None:
    obs.obs_properties_add_text(
        group_obj,
        PROPERTY_NAMES.FALLBACK_WINDOW_NAME,
        "Fallback folder name: ",
        obs.OBS_TEXT_DEFAULT,
    )

    obs.obs_properties_add_text(
        group_obj, PROPERTY_NAMES.REPLAY_FOLDER_NAME, "Replay folder name: ", obs.OBS_TEXT_DEFAULT
    )

    obs.obs_properties_add_text(
        group_obj,
        PROPERTY_NAMES.SCREENSHOT_FOLDER_NAME,
        "Screenshot folder name: ",
        obs.OBS_TEXT_DEFAULT,
    )

    folder_name_source = obs.obs_properties_add_list(
        group_obj,
        PROPERTY_NAMES.FOLDER_NAME_SOURCE,
        "Name folders by: ",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    populate_folder_name_source(folder_name_source)
    obs.obs_property_set_long_description(
        folder_name_source,
        "Use the window title (ex. 'ELDEN RING') or the executable name (ex. 'eldenring') "
        "as the folder name. The other one is used if the preferred one is unusable.",
    )

    hook_memory = obs.obs_properties_add_int(
        group_obj, PROPERTY_NAMES.HOOK_MEMORY_SECONDS, "Remember last game for (s): ", 0, 3600, 10
    )
    obs.obs_property_set_long_description(
        hook_memory,
        "If the game closed (or lost the hook) just before the clip was saved, keep using its "
        "folder for this many seconds. Set to 0 to disable.",
    )

    organize_replay = obs.obs_properties_add_bool(
        group_obj, PROPERTY_NAMES.ENABLE_REPLAY_ORGANIZATION, "Organize Replay Buffer recordings "
    )
    obs.obs_property_set_long_description(
        organize_replay,
        "Check the box, if you want to have replays organized into subfolders, uncheck to disable",
    )

    organize_screenshots = obs.obs_properties_add_bool(
        group_obj, PROPERTY_NAMES.ENABLE_SCREENSHOT_ORGANIZATION, "Organize screenshots "
    )
    obs.obs_property_set_long_description(
        organize_screenshots,
        "Check the box, if you want to have screenshots organized into subfolders, uncheck to disable",
    )

    title_as_prefix = obs.obs_properties_add_bool(
        group_obj, PROPERTY_NAMES.TITLE_AS_PREFIX, "Add game name as a file prefix "
    )
    obs.obs_property_set_long_description(
        title_as_prefix,
        "Check the box, if you want to have title of hooked application appended as a prefix to the recording, else uncheck",
    )

    verbose_logging = obs.obs_properties_add_bool(
        group_obj, PROPERTY_NAMES.VERBOSE_LOGGING, "Verbose logging "
    )
    obs.obs_property_set_long_description(
        verbose_logging,
        "Write extra debug lines to the Script Log. Useful when reporting an issue.",
    )


def setup_core(group_obj) -> None:
    source_selector = obs.obs_properties_add_list(
        group_obj,
        PROPERTY_NAMES.SOURCE_SELECTOR,
        "Monitored source: ",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    populate_source_selector(source_selector)
    obs.obs_property_set_long_description(
        source_selector,
        "The Game/Window Capture source RecORDER asks for the current game. "
        "'(Auto-detect)' picks the best one in the current scene automatically.",
    )

    organization_mode = obs.obs_properties_add_list(
        group_obj,
        PROPERTY_NAMES.ORGANIZATION_MODE,
        "Organization mode: ",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    populate_organization_mode(organization_mode)


@guarded
def check_updates_press(props, prop):
    """
    Button callback. OBS calls button callbacks as cb(props, prop).

    The button toggles: pressing it while the result is on screen just hides it
    again, so a second press does not fire another request at GitHub.
    """
    info_property = obs.obs_properties_get(props, "version_info")
    if info_property is None:
        return True

    if obs.obs_property_visible(info_property):
        obs.obs_property_set_visible(info_property, False)
        return True

    latest_tag = get_latest_release_tag()
    obs.obs_property_set_visible(info_property, True)

    if latest_tag is None:
        description = "Could not reach GitHub - try again later."
    elif is_update_available(BASE_CONSTANTS.VERSION, latest_tag):
        description = "Update available: %s\nHead to GitHub for the latest version!" % latest_tag
    else:
        description = "You have the latest version!"

    obs.obs_property_set_description(info_property, description)
    return True


def setup_updates(group_obj) -> None:
    update_text = obs.obs_properties_add_text(group_obj, "version_info", "", obs.OBS_TEXT_INFO)
    obs.obs_property_set_visible(update_text, False)

    obs.obs_properties_add_button(
        group_obj, "check_updates_button", "Check for updates", check_updates_press
    )


# ============================================================================
# OBS FUNCTIONS
# ============================================================================


@guarded
def script_load(settings):
    global config_manager, script_settings

    script_settings = settings

    if config_manager is None:
        config_manager = ConfigManager(get_config_path())

    # Flush whatever the background threads logged, on the UI thread.
    obs.timer_remove(_flush_deferred_logs)
    obs.timer_add(_flush_deferred_logs, LOG_FLUSH_INTERVAL_MS)

    # Create the shared file-move worker up front so no core has to.
    get_file_move_worker()

    obs.obs_frontend_remove_event_callback(frontend_event_callback)
    obs.obs_frontend_add_event_callback(frontend_event_callback)

    rebuild_core(settings)
    log("info", "RecORDER %s loaded" % BASE_CONSTANTS.VERSION)


@guarded
def script_update(settings):
    global script_settings

    script_settings = settings

    # OBS fires script_update immediately after script_load, and again whenever
    # the properties dialog decides something changed. Rebuilding for identical
    # settings would tear down and re-attach every signal for nothing.
    if core is not None and settings_signature(settings) == _core_settings_signature:
        log("debug", "Settings are unchanged - keeping the current core")
        return

    rebuild_core(settings)
    log("debug", "Configuration updated")


@guarded
def script_defaults(settings):
    obs.obs_data_set_default_string(
        settings, PROPERTY_NAMES.FALLBACK_WINDOW_NAME, DEFAULT_FALLBACK_FOLDER_NAME
    )
    obs.obs_data_set_default_string(
        settings, PROPERTY_NAMES.REPLAY_FOLDER_NAME, DEFAULT_REPLAY_FOLDER_NAME
    )
    obs.obs_data_set_default_string(
        settings, PROPERTY_NAMES.SCREENSHOT_FOLDER_NAME, DEFAULT_SCREENSHOT_FOLDER_NAME
    )
    obs.obs_data_set_default_string(
        settings, PROPERTY_NAMES.ORGANIZATION_MODE, AVAILABLE_ORGANIZATION_MODES.BASIC
    )
    obs.obs_data_set_default_string(settings, PROPERTY_NAMES.SOURCE_SELECTOR, "")
    obs.obs_data_set_default_string(
        settings, PROPERTY_NAMES.FOLDER_NAME_SOURCE, FOLDER_NAME_SOURCES.WINDOW_TITLE
    )
    obs.obs_data_set_default_int(
        settings, PROPERTY_NAMES.HOOK_MEMORY_SECONDS, DEFAULT_HOOK_MEMORY_SECONDS
    )
    obs.obs_data_set_default_bool(settings, PROPERTY_NAMES.TITLE_AS_PREFIX, False)
    obs.obs_data_set_default_bool(settings, PROPERTY_NAMES.ENABLE_REPLAY_ORGANIZATION, True)
    obs.obs_data_set_default_bool(settings, PROPERTY_NAMES.ENABLE_SCREENSHOT_ORGANIZATION, True)
    obs.obs_data_set_default_bool(settings, PROPERTY_NAMES.VERBOSE_LOGGING, False)


@guarded
def script_unload():
    global core, script_settings, _core_settings_signature

    obs.obs_frontend_remove_event_callback(frontend_event_callback)

    if core is not None:
        core.shutdown()
        core = None

    _core_settings_signature = None
    script_settings = None
    log("info", "RecORDER unloaded")

    # The only place the worker is joined: give in-flight moves a bounded
    # chance to finish, then stop flushing logs through OBS.
    shutdown_file_move_worker(WORKER_JOIN_TIMEOUT_SECONDS)

    _flush_deferred_logs()
    try:
        obs.timer_remove(_flush_deferred_logs)
    except Exception:
        pass


def script_properties():
    try:
        props = obs.obs_properties_create()

        customization_gr = obs.obs_properties_create()
        core_gr = obs.obs_properties_create()
        update_gr = obs.obs_properties_create()

        obs.obs_properties_add_group(
            props, "core_group", "Core settings:", obs.OBS_GROUP_NORMAL, core_gr
        )
        obs.obs_properties_add_group(
            props,
            "available_customization_group",
            "Available customizations:",
            obs.OBS_GROUP_NORMAL,
            customization_gr,
        )
        obs.obs_properties_add_group(
            props, "update_group", "Update the script:", obs.OBS_GROUP_NORMAL, update_gr
        )

        setup_customization(customization_gr)
        setup_core(core_gr)
        setup_updates(update_gr)

        return props
    except Exception:
        log_exception("script_properties")
        return obs.obs_properties_create()


def script_description():
    return f"""
        <div style="font-size: 40pt; text-align: center;"> RecORDER <i>{BASE_CONSTANTS.VERSION}</i> </div>
        <hr>
        <div style="font-size: 12pt; text-align: left;">
        Rename and organize media into subfolders!<br>
        <i>Similar to ShadowPlay (GeForce Experience</i>).
        </div>
        <div style="font-size: 12pt; text-align: left; margin-top: 20px; margin-bottom: 20px;">
        Original script by oxypatic (<a href="https://github.com/oxypatic/RecORDER">oxypatic/RecORDER</a>).<br>
        This fork: <a href="https://github.com/Junnn888/obs-replay-folder-organisation">Junnn888/obs-replay-folder-organisation</a>
        </div>
    """
