from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, CONF_SERIAL_NUMBER, CONF_ENTITY_IDENTITY, CONF_ADVERTISED_NAME
from .coordinator import NubertSpeakerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.MEDIA_PLAYER,
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.BUTTON,
]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:  # type: ignore[arg-type]
    """Set up the integration via YAML (not supported)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Nubert speaker from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    address: str | None = entry.data.get(CONF_ADDRESS)
    if address is None:
        _LOGGER.error("Config entry missing address")
        raise ConfigEntryNotReady

    identity = entry.data.get(CONF_ENTITY_IDENTITY, entry.unique_id or address)
    coordinator = NubertSpeakerCoordinator(
        hass, address, entry.title, identity, config_entry_id=entry.entry_id
    )
    try:
        # Connects, detects the model (IS_SLAVE) and reads power/volume/source.
        await coordinator.async_config_entry_first_refresh()
        serial = await coordinator.async_read_serial_number()
        expected = entry.data.get(CONF_SERIAL_NUMBER)
        if expected and serial != expected:
            raise ConfigEntryNotReady("Speaker serial number did not match")
        updated_data = dict(entry.data)
        new_unique_id = None
        if serial and not expected:
            serial_id = f"serial:{serial}"
            other = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, serial_id)
            if other is None or other.entry_id == entry.entry_id:
                new_unique_id = serial_id
                updated_data[CONF_SERIAL_NUMBER] = serial
                updated_data[CONF_ENTITY_IDENTITY] = identity
            else:
                _LOGGER.warning("Serial number collision for %s", address)
        if not entry.data.get(CONF_ADVERTISED_NAME):
            info = bluetooth.async_last_service_info(hass, address, connectable=True)
            if info and info.name and info.name.strip() != address:
                updated_data[CONF_ADVERTISED_NAME] = info.name.strip()
        if updated_data != entry.data or new_unique_id is not None:
            if new_unique_id is not None:
                hass.config_entries.async_update_entry(
                    entry, data=updated_data, unique_id=new_unique_id
                )
            else:
                hass.config_entries.async_update_entry(entry, data=updated_data)
    except Exception as err:
        await coordinator.async_disconnect()
        raise ConfigEntryNotReady from err

    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator}

    # Re-render the media player when options change; its settings are read live.
    async def _options_updated(_hass: HomeAssistant, _entry: ConfigEntry) -> None:
        coordinator.async_update_listeners()

    entry.async_on_unload(entry.add_update_listener(_options_updated))

    # Platforms register the settings their entities need; read them now.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_refresh_settings()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        if (stored := hass.data[DOMAIN].pop(entry.entry_id, None)) and "coordinator" in stored:
            await stored["coordinator"].async_disconnect()

    return unload_ok
