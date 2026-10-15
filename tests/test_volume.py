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
