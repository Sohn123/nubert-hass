"""Focused volume tests without a full Home Assistant installation."""

import asyncio
import importlib.util
from enum import IntFlag
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "nubert"


def _module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
    return module


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def modules(monkeypatch):
    package = _module("custom_components.nubert", __path__=[str(ROOT)])
    _module("custom_components", __path__=[str(ROOT.parent)])
    protocol = _load("custom_components.nubert.protocol", "protocol.py")
    const = _load("custom_components.nubert.const", "const.py")
    volume = _load("custom_components.nubert.volume", "volume.py")

    class Features(IntFlag):
        TURN_ON = 1
        TURN_OFF = 2
        VOLUME_SET = 4
        VOLUME_STEP = 8
        SELECT_SOURCE = 16
        VOLUME_MUTE = 32

    class Entity:
        def __init__(self, coordinator, key):
            self.coordinator = coordinator
            self.state_writes = 0

        def async_write_ha_state(self):
            self.state_writes += 1

    _module("homeassistant", __path__=[])
    _module("homeassistant.components", __path__=[])
    _module("homeassistant.components.media_player", __path__=[], MediaPlayerEntity=object, MediaPlayerEntityFeature=Features)
    _module("homeassistant.components.media_player.const", MediaPlayerState=types.SimpleNamespace(ON="on", OFF="off"))
    _module("homeassistant.config_entries", ConfigEntry=object)
    _module("homeassistant.const", ATTR_SUGGESTED_AREA="suggested_area")
    _module("homeassistant.core", HomeAssistant=object)
    _module("homeassistant.helpers", __path__=[])
    _module("homeassistant.helpers.device_registry", DeviceInfo=dict)
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    _module("custom_components.nubert.coordinator", NubertSpeakerCoordinator=object)
    _module("custom_components.nubert.entity", NubertEntity=Entity)
    player = _load("custom_components.nubert.media_player", "media_player.py")
    yield protocol, const, volume, player
    for name in list(sys.modules):
        if (name == "homeassistant" or name.startswith("homeassistant.")
                or name == "custom_components" or name.startswith("custom_components.")
                or name == "bleak" or name.startswith("bleak.")
                or name == "bleak_retry_connector"):
            monkeypatch.delitem(sys.modules, name, raising=False)


class Speaker:
    address = "test"
    identity = "test"
    name = "test"
    power = True
    is_slave = False
    source_names = {}
    source_code = None
    model = "x2"

    def __init__(self, protocol, raw=50, muted=False):
        self.p = protocol
        self.raw = {protocol.VOLUME_GET: bytes([raw]), protocol.MUTE_GET: bytes([int(muted)])}
        self.sent = []

    @property
    def muted(self):
        return bool(self.value(self.p.MUTE_GET))

    @property
    def volume_db(self):
        return self.value(self.p.VOLUME_GET) - 100

    def value(self, command, index=0):
        payload = self.raw.get(command)
        return payload[index] if payload and len(payload) > index else None

    async def async_set(self, get, set_command, value):
        self.sent.append((set_command, value))
        self.raw[get] = bytes([value])


def make_player(modules, raw=50, muted=False, options=None, model="x2"):
    p, _, _, media = modules
    speaker = Speaker(p, raw, muted)
    speaker.model = model
    entry = types.SimpleNamespace(options=options or {})
    return media.NubertMediaPlayer(speaker, None, entry), speaker


def test_defaults_and_model_range(modules):
    p, _, v, _ = modules
    assert v.default_db_range(p.X2) == (-70, -30)
    assert v.default_db_range(p.Z1) == (-14, 6)
    assert v.supported_db_range(p.XSUB) == (-15, 6)
    player, _ = make_player(modules)
    assert player._volume_settings == (-70, -30, 1.0)


def test_mapping_bounds_curve_and_round_trip(modules):
    p, _, v, _ = modules
    assert v.level_to_raw(-1, -70, -30, 1, p.X2) == 30
    assert v.level_to_raw(1.5, -70, -30, 1, p.X2) == 70
    assert v.level_to_raw(0.5, -70, -30, 1, p.X2) == 50
    assert v.level_to_raw(0.5, -70, -30, 2, p.X2) == 40
    assert v.raw_to_level(40, -70, -30, 2, p.X2) == pytest.approx(0.5)
    assert v.raw_to_level(0, -70, -30, 1, p.X2) == 0
    assert v.raw_to_level(100, -70, -30, 1, p.X2) == 1
    assert v.level_to_raw(0.5, -14, 6, 1, p.Z1) == 10
    for raw in range(30, 71):
        level = v.raw_to_level(raw, -70, -30, 1, p.X2)
        assert v.level_to_raw(level, -70, -30, 1, p.X2) == raw


def test_quantization_and_reported_level(modules):
    player, speaker = make_player(modules)
    asyncio.run(player.async_set_volume_level(0.51))
    assert speaker.sent == []
    # A non-integral request is rounded once to the protocol's integer byte.
    asyncio.run(player.async_set_volume_level(0.54))
    assert speaker.value(speaker.p.VOLUME_GET) == 52
    assert player.volume_level == pytest.approx(0.55)
    p, _, v, _ = modules
    assert v.level_to_raw(1, -70, -30.5, 1, p.X2) == 69


def test_zero_and_explicit_mute(modules):
    player, speaker = make_player(modules)
    asyncio.run(player.async_set_volume_level(0))
    assert speaker.sent == [(speaker.p.MUTE_SET, 1)]
    assert player.volume_level == 0
    asyncio.run(player.async_set_volume_level(0.5))
    assert speaker.sent[-1] == (speaker.p.MUTE_SET, 0)
    asyncio.run(player.async_mute_volume(True))
    asyncio.run(player.async_set_volume_level(0.7))
    assert speaker.muted is True
    assert speaker.sent[-1] == (speaker.p.VOLUME_SET, 58)


def test_limits_steps_and_external_changes(modules):
    player, speaker = make_player(modules, raw=70)
    asyncio.run(player.async_volume_up())
    assert speaker.sent == []
    asyncio.run(player.async_set_volume_level(2))
    assert speaker.sent == []
    speaker.raw[speaker.p.VOLUME_GET] = bytes([80])  # physical remote
    assert player.volume_level == 1
    asyncio.run(player.async_volume_up())
    assert speaker.sent == []
    asyncio.run(player.async_volume_down())
    assert speaker.sent[-1] == (speaker.p.VOLUME_SET, 70)
    speaker.raw[speaker.p.VOLUME_GET] = bytes([50])
    assert player.volume_level == pytest.approx(0.5)
    asyncio.run(player.async_volume_up())
    assert speaker.sent[-1] == (speaker.p.VOLUME_SET, 52)
    speaker.raw[speaker.p.VOLUME_GET] = bytes([20])
    count = len(speaker.sent)
    asyncio.run(player.async_volume_down())
    assert len(speaker.sent) == count


def test_options_change_applies_to_existing_player(modules):
    player, speaker = make_player(modules, raw=50)
    assert player.volume_level == pytest.approx(0.5)
    player._entry.options = {
        "min_volume_db": -60,
        "max_volume_db": -20,
        "volume_gamma": 2.0,
    }
    assert player.volume_level == pytest.approx((10 / 40) ** 0.5)
    asyncio.run(player.async_set_volume_level(0.75))
    assert speaker.sent[-1] == (speaker.p.VOLUME_SET, 62)


def test_zero_step_restores_minimum(modules):
    player, speaker = make_player(modules, raw=60)
    asyncio.run(player.async_set_volume_level(0))
    asyncio.run(player.async_volume_up())
    assert speaker.sent[-2:] == [(speaker.p.VOLUME_SET, 30), (speaker.p.MUTE_SET, 0)]


def test_remote_unmute_clears_zero_step_behavior(modules):
    player, speaker = make_player(modules, raw=60)
    asyncio.run(player.async_set_volume_level(0))
    speaker.raw[speaker.p.MUTE_GET] = bytes([0])
    assert player.volume_level == pytest.approx(0.75)
    asyncio.run(player.async_volume_up())
    assert speaker.sent[-1] == (speaker.p.VOLUME_SET, 62)


def test_invalid_settings(modules):
    p, _, v, _ = modules
    assert v.valid_settings(-70, -30, 1, p.X2)
    for settings in [(-30, -30, 1), (-20, -40, 1), (-101, -30, 1),
                     (-70, 1, 1), (-70, -30, 0), (-70, -30, 3.1)]:
        assert not v.valid_settings(*settings, p.X2)
    assert not v.valid_settings(-70, -30, 1, p.Z1)


def test_serial_frames_and_conservative_validation(modules):
    p, _, _, _ = modules
    payload = b"SN12345"
    assert p.parse_frames(p.frame(p.SERIAL_NUMBER_GET, *payload)) == [
        (p.SERIAL_NUMBER_GET, payload)
    ]
    wrapped = p.frame(p.NEW_INFO_GET, *p.frame(p.SERIAL_NUMBER_GET, *payload))
    assert p.parse_frames(wrapped) == [(p.SERIAL_NUMBER_GET, payload)]
    assert p.serial_number(payload) == payload.hex()
    for invalid in (None, b"", b"\x00" * 8, b"\xff" * 8, b"A" * 33):
        assert p.serial_number(invalid) is None


def test_serial_discovery_reuses_entry_and_relinks_legacy(modules):
    """A changed address updates an entry without replacing its entity identity."""

    class FlowBase:
        def __init_subclass__(cls, **kwargs):
            pass

        async def async_set_unique_id(self, unique_id):
            self.unique_id = unique_id

        def _async_current_entries(self, include_ignore=False):
            return self.hass.config_entries.entries

        def async_abort(self, reason):
            return {"abort": reason}

        def async_create_entry(self, title, data):
            return {"title": title, "data": data}

        def async_show_form(self, step_id, data_schema=None, **kwargs):
            return {"step_id": step_id}

    class Probe:
        serial = "534e313233"

        def __init__(self, hass, address, name):
            pass

        async def async_read_serial_number(self):
            return self.serial

        async def async_disconnect(self):
            pass

    class Entries:
        def __init__(self, entry):
            self.entries = [entry]
            self.reloaded = []

        def async_entry_for_domain_unique_id(self, domain, unique_id):
            return next((entry for entry in self.entries if entry.unique_id == unique_id), None)

        def async_update_entry(self, entry, *, data, unique_id=None):
            entry.data = data
            if unique_id is not None:
                entry.unique_id = unique_id

        async def async_reload(self, entry_id):
            self.reloaded.append(entry_id)

    class Select:
        def __init__(self, config):
            pass

        def __call__(self, value):
            return value

    select = _module(
        "homeassistant.helpers.selector",
        SelectSelector=Select,
        SelectSelectorConfig=lambda **kwargs: kwargs,
        SelectSelectorMode=types.SimpleNamespace(DROPDOWN="dropdown"),
        SelectOptionDict=lambda **kwargs: kwargs,
        NumberSelector=Select,
        NumberSelectorConfig=lambda **kwargs: kwargs,
        NumberSelectorMode=types.SimpleNamespace(BOX="box"),
    )
    sys.modules["homeassistant.helpers"].selector = select
    _module("homeassistant.components.bluetooth", BluetoothServiceInfo=object,
            async_discovered_service_info=lambda hass: [],
            async_last_service_info=lambda hass, address, connectable: None)
    _module("homeassistant.config_entries", ConfigEntry=object, ConfigFlow=FlowBase,
            ConfigFlowResult=dict, OptionsFlow=object)
    _module("homeassistant.const", CONF_ADDRESS="address", CONF_NAME="name")
    _module("custom_components.nubert.coordinator", NubertSpeakerCoordinator=Probe)
    flow_module = _load("custom_components.nubert.config_flow", "config_flow.py")

    entry = types.SimpleNamespace(
        entry_id="old-entry", unique_id="OLD:ADDRESS", title="Left",
        data={"address": "OLD:ADDRESS", "entity_identity": "OLD:ADDRESS"},
    )
    hass = types.SimpleNamespace(config_entries=Entries(entry), data={})
    flow = flow_module.NubertConfigFlow()
    flow.hass = hass
    assert asyncio.run(flow._async_identify("NEW:ADDRESS")) is None
    assert asyncio.run(flow.async_step_link_or_add({"existing_entry": "old-entry"})) == {
        "abort": "relinked"
    }
    assert entry.unique_id == "serial:534e313233"
    assert entry.data["address"] == "NEW:ADDRESS"
    assert entry.data["entity_identity"] == "OLD:ADDRESS"
    assert hass.config_entries.reloaded == ["old-entry"]

    next_flow = flow_module.NubertConfigFlow()
    next_flow.hass = hass
    assert asyncio.run(next_flow._async_identify("ANOTHER:ADDRESS")) == {
        "abort": "already_configured"
    }
    assert entry.data["address"] == "ANOTHER:ADDRESS"
    assert entry.data["entity_identity"] == "OLD:ADDRESS"

    hass.data = {"nubert": {entry.entry_id: {"coordinator": types.SimpleNamespace(connected=True)}}}
    collision = flow_module.NubertConfigFlow()
    collision.hass = hass
    assert asyncio.run(collision._async_identify("OTHER:ADDRESS")) == {
        "abort": "serial_collision"
    }
    assert entry.data["address"] == "ANOTHER:ADDRESS"

    Probe.serial = None
    legacy = types.SimpleNamespace(
        entry_id="legacy-entry", unique_id="OLD:ADDRESS", title="Right",
        data={"address": "OLD:ADDRESS"},
    )
    hass.config_entries.entries = [legacy]
    hass.data = {"nubert": {legacy.entry_id: {"coordinator": types.SimpleNamespace(connected=False)}}}
    unknown = flow_module.NubertConfigFlow()
    unknown.hass = hass
    unknown.context = {}
    discovered = types.SimpleNamespace(
        address="UNKNOWN:ADDRESS", name="Nubert",
        service_uuids=[next(iter(modules[1].ADV_UUIDS))],
    )
    assert asyncio.run(unknown.async_step_bluetooth(discovered)) == {
        "step_id": "link_or_add"
    }
    assert asyncio.run(unknown.async_step_link_or_add({"existing_entry": legacy.entry_id})) == {
        "abort": "relinked"
    }
    assert legacy.unique_id == "OLD:ADDRESS"
    assert legacy.data["address"] == "UNKNOWN:ADDRESS"
    assert legacy.data["entity_identity"] == "OLD:ADDRESS"
    assert "serial_number" not in legacy.data
    assert hass.config_entries.reloaded[-1] == legacy.entry_id

    repeat = flow_module.NubertConfigFlow()
    repeat.hass = hass
    assert asyncio.run(repeat.async_step_bluetooth(discovered)) == {
        "abort": "already_configured"
    }

    new_speaker = flow_module.NubertConfigFlow()
    new_speaker.hass = hass
    assert asyncio.run(new_speaker._async_identify("BRAND:NEW")) is None
    assert asyncio.run(new_speaker.async_step_link_or_add({"existing_entry": "new"})) == {
        "title": "BRAND:NEW", "data": {"address": "BRAND:NEW"}
    }

    named = types.SimpleNamespace(
        entry_id="named-entry", unique_id="OLD:ADDRESS", title="Living Room Right",
        data={"address": "OLD:ADDRESS", "advertised_name": "nuPro Right"},
    )
    hass.config_entries.entries = [named]
    hass.data = {"nubert": {named.entry_id: {"coordinator": types.SimpleNamespace(connected=False)}}}
    by_name = flow_module.NubertConfigFlow()
    by_name.hass = hass
    candidate = types.SimpleNamespace(
        address="NEW:ADDRESS", name="nuPro Right",
        service_uuids=discovered.service_uuids, connectable=True,
    )
    assert asyncio.run(by_name.async_step_bluetooth(candidate)) == {"abort": "relinked"}
    assert named.unique_id == "OLD:ADDRESS"
    assert named.data["address"] == "NEW:ADDRESS"
    assert named.data["advertised_name"] == "nuPro Right"

    hass.data["nubert"][named.entry_id]["coordinator"].connected = True
    by_name._candidate_name = "nuPro Right"
    assert by_name._matching_name_entry() is None
    hass.data["nubert"][named.entry_id]["coordinator"].connected = False

    duplicate = types.SimpleNamespace(
        entry_id="duplicate", unique_id="OTHER:ADDRESS", title="Other",
        data={"address": "OTHER:ADDRESS", "advertised_name": "nuPro Right"},
    )
    hass.config_entries.entries.append(duplicate)
    ambiguous = flow_module.NubertConfigFlow()
    ambiguous.hass = hass
    ambiguous._candidate_name = "nuPro Right"
    assert ambiguous._matching_name_entry() is None

    first_setup = flow_module.NubertConfigFlow()
    first_setup._discovery_info = candidate
    first_setup._candidate_name = "nuPro Right"
    first_setup._serial_number = None
    assert asyncio.run(first_setup.async_step_bluetooth_confirm({"name": "Living Room"})) == {
        "title": "Living Room",
        "data": {"address": "NEW:ADDRESS", "advertised_name": "nuPro Right"},
    }

    older = types.SimpleNamespace(
        entry_id="older", unique_id="PREVIOUS:ADDRESS", title="nuPro Right",
        data={"address": "PREVIOUS:ADDRESS"},
    )
    hass.config_entries.entries = [older]
    hass.data = {"nubert": {older.entry_id: {"coordinator": types.SimpleNamespace(connected=False)}}}
    migrated = flow_module.NubertConfigFlow()
    migrated.hass = hass
    assert asyncio.run(migrated.async_step_bluetooth(candidate)) == {"abort": "relinked"}
    assert older.data["address"] == "NEW:ADDRESS"
    assert older.data["advertised_name"] == "nuPro Right"


def test_coordinator_requires_two_fresh_matching_serial_replies(modules):
    p, _, _, _ = modules

    class GenericCoordinator:
        def __class_getitem__(cls, item):
            return cls

    _module("bleak", __path__=[])
    _module("bleak.backends", __path__=[])
    _module("bleak.backends.characteristic", BleakGATTCharacteristic=object)
    _module("bleak.exc", BleakError=Exception)
    _module("bleak_retry_connector", BleakClientWithServiceCache=object,
            establish_connection=lambda *args, **kwargs: None)
    _module("homeassistant.components.bluetooth", async_ble_device_from_address=lambda *args, **kwargs: None)
    _module("homeassistant.core", HomeAssistant=object, callback=lambda fn: fn)
    _module("homeassistant.helpers.device_registry", async_get=lambda hass: None)
    _module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=GenericCoordinator,
            UpdateFailed=Exception)
    module = _load("custom_components.nubert.coordinator", "coordinator.py")
    coordinator = object.__new__(module.NubertSpeakerCoordinator)
    coordinator._poll_lock = asyncio.Lock()
    coordinator.address = "test"
    coordinator.device = object()
    coordinator.raw = {}
    replies = [b"ABC123", b"ABC123"]

    async def connected():
        pass

    async def query(commands):
        assert commands == [p.SERIAL_NUMBER_GET]
        coordinator.raw[p.SERIAL_NUMBER_GET] = replies.pop(0)

    coordinator._ensure_connected = connected
    coordinator._query = query
    assert asyncio.run(coordinator.async_read_serial_number()) == b"ABC123".hex()
    replies[:] = [b"ABC123", b"DEF456"]
    assert asyncio.run(coordinator.async_read_serial_number()) is None
    replies[:] = [b"\x00\x00"]
    assert asyncio.run(coordinator.async_read_serial_number()) is None

    coordinator._pending_event = asyncio.Event()
    coordinator._pending = set()
    replies[:] = [b"ABC123", b"ABC123"]

    async def no_batched_reply(commands):
        assert commands == [p.SERIAL_NUMBER_GET]

    async def bare_reply(frame):
        assert frame == p.frame(p.SERIAL_NUMBER_GET)
        coordinator.raw[p.SERIAL_NUMBER_GET] = replies.pop(0)
        coordinator._pending_event.set()

    coordinator._query = no_batched_reply
    coordinator._write = bare_reply
    assert asyncio.run(coordinator.async_read_serial_number()) == b"ABC123".hex()


def test_device_registry_lookup_uses_scoped_identifier(modules):
    _p, _, _, _media = modules

    class GenericCoordinator:
        def __class_getitem__(cls, item):
            return cls

    _module("bleak", __path__=[])
    _module("bleak.backends", __path__=[])
    _module("bleak.backends.characteristic", BleakGATTCharacteristic=object)
    _module("bleak.exc", BleakError=Exception)
    _module("bleak_retry_connector", BleakClientWithServiceCache=object,
            establish_connection=lambda *args, **kwargs: None)
    _module("homeassistant.components.bluetooth", async_ble_device_from_address=lambda *args, **kwargs: None)
    _module("homeassistant.core", HomeAssistant=object, callback=lambda fn: fn)
    calls = []
    registry = types.SimpleNamespace(
        async_get_device_by_identifier=lambda identifier, entry_id: (
            calls.append((identifier, entry_id)) or types.SimpleNamespace(id="device")
        ),
        async_update_device=lambda device_id, **kwargs: calls.append(device_id),
    )
    _module("homeassistant.helpers.device_registry", async_get=lambda hass: registry)
    _module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=GenericCoordinator,
            UpdateFailed=Exception)
    module = _load("custom_components.nubert.coordinator", "coordinator.py")
    coordinator = object.__new__(module.NubertSpeakerCoordinator)
    coordinator.config_entry_id = "entry"
    coordinator.identity = "old-address"
    coordinator.hass = object()
    coordinator.device = None
    coordinator._is_x1_gatt = False
    coordinator.versions = {}
    coordinator._update_device_registry()
    assert calls[0] == (("nubert", "old-address"), "entry")
    assert calls[1] == "device"
