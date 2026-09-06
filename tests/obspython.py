"""
A hand-written stand-in for OBS' `obspython` binding, good enough to drive
RecORDER.py in a normal Python process.

It deliberately reproduces the parts of the real binding that bite:

* `signal_handler_connect` / `_disconnect` accept ONLY plain functions
  (the real hand-written C wrappers do `PyFunction_Check`, so bound methods
  are silently ignored); disconnect matches on object identity.
* `obs_source_release(None)` is a no-op, every other get/release pair is
  counted so tests can assert we do not leak references.
* `obs_scene_enum_items` returns a list that must be released.
* `obs_frontend_recording_stop` / `_replay_buffer_stop` / `_replay_buffer_save`
  raise AssertionError - RecORDER must never touch the user's recording.
* every `obs_frontend_*` function asserts it is being called while
  `world.current_thread_role == "ui"`. `world.fire_signal(..., "file_changed")`
  flips that role to "muxer" for the duration of the callback, which is exactly
  how the real binding behaves: the muxer thread must never reach the frontend
  API.
* `timer_add` / `timer_remove` store plain functions (a bound method is a bug);
  `world.tick_timers()` runs them the way OBS' UI thread would.
"""

import inspect

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EVENT_NAMES = [
    "OBS_FRONTEND_EVENT_STREAMING_STARTED",
    "OBS_FRONTEND_EVENT_RECORDING_STARTED",
    "OBS_FRONTEND_EVENT_RECORDING_STOPPING",
    "OBS_FRONTEND_EVENT_RECORDING_STOPPED",
    "OBS_FRONTEND_EVENT_SCENE_CHANGED",
    "OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGING",
    "OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGED",
    "OBS_FRONTEND_EVENT_REPLAY_BUFFER_STARTED",
    "OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED",
    "OBS_FRONTEND_EVENT_REPLAY_BUFFER_STOPPED",
    "OBS_FRONTEND_EVENT_SCREENSHOT_TAKEN",
    "OBS_FRONTEND_EVENT_FINISHED_LOADING",
    "OBS_FRONTEND_EVENT_EXIT",
]

for _index, _name in enumerate(_EVENT_NAMES):
    globals()[_name] = 1000 + _index

LOG_ERROR = 100
LOG_WARNING = 200
LOG_INFO = 300
LOG_DEBUG = 400

OBS_TEXT_DEFAULT = 0
OBS_TEXT_INFO = 3
OBS_COMBO_TYPE_LIST = 1
OBS_COMBO_FORMAT_STRING = 1
OBS_GROUP_NORMAL = 1


# ---------------------------------------------------------------------------
# Fake OBS objects
# ---------------------------------------------------------------------------


class SignalHandler:
    """Signal registry keyed by (signal name, callback identity)."""

    def __init__(self, owner_name):
        self.owner_name = owner_name
        self.connections = {}

    def connect(self, name, callback):
        # The real binding does PyFunction_Check() and silently drops anything
        # that is not a plain function (bound methods included).
        if not inspect.isfunction(callback):
            return
        self.connections.setdefault(name, []).append(callback)

    def disconnect(self, name, callback):
        if not inspect.isfunction(callback):
            return
        callbacks = self.connections.get(name, [])
        for index, existing in enumerate(callbacks):
            if existing is callback:
                del callbacks[index]
                return

    def count(self, name):
        return len(self.connections.get(name, []))

    def callbacks(self, name):
        return list(self.connections.get(name, []))


class ProcHandler:
    def __init__(self, source):
        self.source = source


class FakeSource:
    def __init__(
        self,
        source_id,
        name,
        uuid,
        hooked=False,
        title=None,
        executable=None,
        scene=None,
        supports_get_hooked=None,
    ):
        self.id = source_id
        self.name = name
        self.uuid = uuid
        self.hooked = hooked
        self.title = title
        self.executable = executable
        self.scene = scene
        self.handler = SignalHandler(name)
        self.proc_handler = ProcHandler(self)
        if supports_get_hooked is None:
            supports_get_hooked = source_id in (
                "game_capture",
                "window_capture",
                "xcomposite_input",
            )
        self.supports_get_hooked = supports_get_hooked

    def __repr__(self):
        return "<FakeSource %s %s>" % (self.id, self.name)


class FakeSceneItem:
    def __init__(self, source, group_scene=None):
        self.source = source
        self.group_scene = group_scene

    @property
    def is_group(self):
        return self.group_scene is not None


class FakeScene:
    def __init__(self, name, uuid=None):
        self.name = name
        self.items = []
        self.source = FakeSource("scene", name, uuid or ("uuid-scene-" + name), scene=self)

    def add(self, source):
        item = FakeSceneItem(source)
        self.items.append(item)
        return item

    def add_group(self, group_source, group_scene):
        item = FakeSceneItem(group_source, group_scene=group_scene)
        self.items.append(item)
        return item


class FakeOutput:
    def __init__(self, name):
        self.name = name
        self.handler = SignalHandler(name)


class FakeConfig:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_bool(self, section, key):
        return bool(self.values.get((section, key), False))


class CallData(dict):
    """Stand-in for calldata_t - a plain string/bool bag."""

    def __init__(self, values=None):
        super().__init__(values or {})
        self.destroyed = False


class FakeData:
    """Stand-in for obs_data_t."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.defaults = {}

    def get(self, key, kind):
        if key in self.values:
            return self.values[key]
        if key in self.defaults:
            return self.defaults[key]
        return {"string": "", "bool": False, "int": 0}[kind]


class FakeProperty:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description
        self.visible = True
        self.long_description = ""
        self.items = []
        self.callback = None
        self.modified_callback = None


class FakeProperties:
    def __init__(self):
        self.properties = {}
        self.order = []

    def add(self, prop):
        self.properties[prop.name] = prop
        self.order.append(prop)
        return prop


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


class World:
    def __init__(self):
        self.sources = {}
        self.current_scene = None
        self.scene_collection = "Untitled"
        self.event_callbacks = []
        self.log_lines = []

        self.last_recording = ""
        self.last_replay = ""
        self.last_screenshot = ""

        self.recording_output = FakeOutput("recording")
        self.recording_active = False
        self.replay_buffer_active = False

        self.profile_config = FakeConfig()

        self.source_refs = 0
        self.output_refs = 0
        self.item_lists_outstanding = 0

        # "ui" or "muxer" - see _assert_ui_thread() below.
        self.current_thread_role = "ui"

        # [(callback, interval_ms)] registered through timer_add.
        self.timers = []

        # Anything RecORDER is not allowed to do lands here (and raises).
        self.forbidden_calls = []

    # -- construction helpers ------------------------------------------------

    def add_source(self, source):
        self.sources[source.uuid] = source
        return source

    def new_source(self, source_id, name, uuid, **kwargs):
        return self.add_source(FakeSource(source_id, name, uuid, **kwargs))

    def new_scene(self, name, uuid=None):
        scene = FakeScene(name, uuid=uuid)
        self.add_source(scene.source)
        return scene

    def remove_source(self, source):
        self.sources.pop(source.uuid, None)

    def set_current_scene(self, scene):
        self.current_scene = scene

    # -- behaviour helpers ---------------------------------------------------

    def fire_event(self, event):
        for callback in list(self.event_callbacks):
            callback(event)

    def fire_signal(self, source, name, **values):
        calldata = CallData(values)

        # `file_changed` is emitted by the muxer thread in real OBS.
        previous_role = self.current_thread_role
        if name == "file_changed":
            self.current_thread_role = "muxer"
        try:
            for callback in source.handler.callbacks(name):
                callback(calldata)
        finally:
            self.current_thread_role = previous_role

    def tick_timers(self):
        """Run every registered timer callback once, like OBS' UI thread would."""
        for callback, _interval in list(self.timers):
            callback()

    def hook(self, source, title, executable=None):
        source.hooked = True
        source.title = title
        source.executable = executable
        self.fire_signal(
            source, "hooked", title=title, executable=executable, **{"class": "UnityWndClass"}
        )

    def unhook(self, source):
        source.hooked = False
        self.fire_signal(source, "unhooked")

    def fire_file_changed(self, next_file):
        self.fire_signal(self.recording_output, "file_changed", next_file=next_file)


world = World()


def reset_world():
    """Start every test from a clean slate."""
    global world
    world = World()
    return world


# ---------------------------------------------------------------------------
# obspython API surface
# ---------------------------------------------------------------------------


def _assert_ui_thread(function_name):
    """Every obs_frontend_* function is UI-thread only in real OBS."""
    if world.current_thread_role != "ui":
        world.forbidden_calls.append(function_name)
        raise AssertionError(
            "%s() was called from the %s thread - obs_frontend_* is UI-thread only"
            % (function_name, world.current_thread_role)
        )


def script_log(level, message):
    world.log_lines.append((level, message))


# -- timers ------------------------------------------------------------------


def timer_add(callback, milliseconds):
    # The real binding wants a plain callable it can hold on to; a bound method
    # keeps the whole object alive (and cannot be removed by identity later).
    assert inspect.isfunction(callback), "obs.timer_add() needs a plain function"
    world.timers.append((callback, milliseconds))


def timer_remove(callback):
    world.timers[:] = [
        (existing, interval) for existing, interval in world.timers if existing is not callback
    ]


# -- sources -----------------------------------------------------------------


def obs_get_source_by_uuid(uuid):
    source = world.sources.get(uuid)
    if source is None:
        return None
    world.source_refs += 1
    return source


def obs_source_release(source):
    if source is None:
        return
    world.source_refs -= 1


def obs_source_get_name(source):
    return source.name if source is not None else None


def obs_source_get_id(source):
    return source.id if source is not None else None


def obs_source_get_uuid(source):
    return source.uuid if source is not None else None


def obs_source_get_signal_handler(source):
    return source.handler


def obs_source_get_proc_handler(source):
    return source.proc_handler


def obs_source_showing(source):
    return True


# -- signals -----------------------------------------------------------------


def signal_handler_connect(handler, name, callback):
    if handler is None:
        return
    handler.connect(name, callback)


def signal_handler_disconnect(handler, name, callback):
    if handler is None:
        return
    handler.disconnect(name, callback)


# -- calldata / procs --------------------------------------------------------


def calldata_create():
    return CallData()


def calldata_destroy(calldata):
    if calldata is not None:
        calldata.destroyed = True


def calldata_string(calldata, name):
    value = calldata.get(name)
    return value if isinstance(value, str) else None


def calldata_bool(calldata, name):
    return bool(calldata.get(name, False))


def proc_handler_call(proc_handler, name, calldata):
    if proc_handler is None:
        return False

    source = proc_handler.source
    if name != "get_hooked" or not source.supports_get_hooked:
        return False

    calldata["hooked"] = bool(source.hooked)
    if source.hooked:
        if source.id == "xcomposite_input":
            calldata["name"] = source.title
        else:
            calldata["title"] = source.title
        calldata["executable"] = source.executable
    return True


# -- scenes ------------------------------------------------------------------


def obs_frontend_get_current_scene():
    _assert_ui_thread("obs_frontend_get_current_scene")
    if world.current_scene is None:
        return None
    world.source_refs += 1
    return world.current_scene.source


def obs_frontend_get_current_scene_collection():
    _assert_ui_thread("obs_frontend_get_current_scene_collection")
    return world.scene_collection


def obs_scene_from_source(source):
    if source is None:
        return None
    return getattr(source, "scene", None)


def obs_scene_enum_items(scene):
    if scene is None:
        return []
    world.item_lists_outstanding += 1
    return list(scene.items)


def sceneitem_list_release(items):
    world.item_lists_outstanding -= 1


def obs_sceneitem_get_source(item):
    return item.source


def obs_sceneitem_is_group(item):
    return item.is_group


def obs_sceneitem_group_get_scene(item):
    return item.group_scene


# -- frontend ----------------------------------------------------------------


def obs_frontend_add_event_callback(callback):
    _assert_ui_thread("obs_frontend_add_event_callback")
    world.event_callbacks.append(callback)


def obs_frontend_remove_event_callback(callback):
    _assert_ui_thread("obs_frontend_remove_event_callback")
    world.event_callbacks[:] = [cb for cb in world.event_callbacks if cb is not callback]


def obs_frontend_get_last_recording():
    _assert_ui_thread("obs_frontend_get_last_recording")
    return world.last_recording


def obs_frontend_get_last_replay():
    _assert_ui_thread("obs_frontend_get_last_replay")
    return world.last_replay


def obs_frontend_get_last_screenshot():
    _assert_ui_thread("obs_frontend_get_last_screenshot")
    return world.last_screenshot


def obs_frontend_get_recording_output():
    _assert_ui_thread("obs_frontend_get_recording_output")
    if world.recording_output is None:
        return None
    world.output_refs += 1
    return world.recording_output


def obs_output_release(output):
    if output is None:
        return
    world.output_refs -= 1


def obs_output_get_signal_handler(output):
    return output.handler


def obs_frontend_recording_active():
    _assert_ui_thread("obs_frontend_recording_active")
    return world.recording_active


def obs_frontend_replay_buffer_active():
    _assert_ui_thread("obs_frontend_replay_buffer_active")
    return world.replay_buffer_active


def obs_frontend_recording_stop():
    _assert_ui_thread("obs_frontend_recording_stop")
    world.forbidden_calls.append("obs_frontend_recording_stop")
    raise AssertionError("RecORDER must never stop the user's recording")


def obs_frontend_replay_buffer_stop():
    _assert_ui_thread("obs_frontend_replay_buffer_stop")
    world.forbidden_calls.append("obs_frontend_replay_buffer_stop")
    raise AssertionError("RecORDER must never stop the replay buffer")


def obs_frontend_replay_buffer_save():
    _assert_ui_thread("obs_frontend_replay_buffer_save")
    world.forbidden_calls.append("obs_frontend_replay_buffer_save")
    raise AssertionError("RecORDER must never force-save the replay buffer")


def obs_frontend_get_profile_config():
    _assert_ui_thread("obs_frontend_get_profile_config")
    return world.profile_config


def config_get_bool(config, section, key):
    if config is None:
        return False
    return config.get_bool(section, key)


# -- obs_data ----------------------------------------------------------------


def obs_data_get_string(settings, name):
    return settings.get(name, "string")


def obs_data_get_bool(settings, name):
    return settings.get(name, "bool")


def obs_data_get_int(settings, name):
    return settings.get(name, "int")


def obs_data_set_default_string(settings, name, value):
    settings.defaults[name] = value


def obs_data_set_default_bool(settings, name, value):
    settings.defaults[name] = value


def obs_data_set_default_int(settings, name, value):
    settings.defaults[name] = value


# -- properties --------------------------------------------------------------


def obs_properties_create():
    return FakeProperties()


def obs_properties_add_text(props, name, description, text_type):
    return props.add(FakeProperty(name, description))


def obs_properties_add_bool(props, name, description):
    return props.add(FakeProperty(name, description))


def obs_properties_add_int(props, name, description, minimum, maximum, step):
    return props.add(FakeProperty(name, description))


def obs_properties_add_list(props, name, description, combo_type, combo_format):
    return props.add(FakeProperty(name, description))


def obs_properties_add_button(props, name, description, callback):
    prop = props.add(FakeProperty(name, description))
    prop.callback = callback
    return prop


def obs_properties_add_group(props, name, description, group_type, group):
    prop = props.add(FakeProperty(name, description))
    prop.group = group
    return prop


def obs_properties_get(props, name):
    return props.properties.get(name)


def obs_property_list_add_string(prop, name, value):
    prop.items.append((name, value))


def obs_property_set_long_description(prop, description):
    prop.long_description = description


def obs_property_set_description(prop, description):
    prop.description = description


def obs_property_set_visible(prop, visible):
    prop.visible = visible


def obs_property_visible(prop):
    return prop.visible


def obs_property_set_modified_callback(prop, callback):
    prop.modified_callback = callback
