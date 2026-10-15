from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (  # type: ignore
    BluetoothServiceInfo,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.helpers import selector
import voluptuous as vol

from .const import (
    DOMAIN,
    ADV_UUIDS,
    ADV_UUIDS_EXTRA,
    CONF_MIN_VOLUME_DB,
    CONF_MAX_VOLUME_DB,
    CONF_VOLUME_GAMMA,
    DEFAULT_VOLUME_GAMMA,
    CONF_SERIAL_NUMBER,
    CONF_ENTITY_IDENTITY,
)
from .coordinator import NubertSpeakerCoordinator
from .volume import default_db_range, supported_db_range, valid_settings

_LOGGER = logging.getLogger(__name__)


class NubertConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nubert BLE speakers."""

    VERSION = 1

    _discovery_info: BluetoothServiceInfo | None = None
    _serial_number: str | None = None
    _candidate_address: str | None = None
    _pending_name: str | None = None

    @staticmethod
    def async_get_options_flow(config_entry) -> NubertOptionsFlow:
        return NubertOptionsFlow(config_entry)

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfo
    ) -> ConfigFlowResult:
        """Handle a flow initialized by bluetooth discovery."""

        if not _matches_nubert(discovery_info):
            return self.async_abort(reason="not_nubert")

        self._discovery_info = discovery_info
        result = await self._async_identify(discovery_info.address, discovery=True)
        if result is not None:
            return result

        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }

        if self._serial_number and self._legacy_entries():
            return await self.async_step_link_or_add()

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered device."""

        assert self._discovery_info is not None
        title_default = self._discovery_info.name or self._discovery_info.address

        if user_input is not None:
            name = user_input.get(CONF_NAME) or title_default
            data = {CONF_ADDRESS: self._discovery_info.address}
            if self._serial_number:
                data[CONF_SERIAL_NUMBER] = self._serial_number
                data[CONF_ENTITY_IDENTITY] = self.unique_id
            return self.async_create_entry(
                title=name,
                data=data,
            )

        self._set_confirm_only()
        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME, default=title_default): str,
            }
        )
        return self.async_show_form(step_id="bluetooth_confirm", data_schema=schema)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user manually initiating a flow."""

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            name = user_input.get(CONF_NAME, address)
            result = await self._async_identify(address)
            if result is not None:
                return result
            self._pending_name = name
            if self._serial_number and self._legacy_entries():
                return await self.async_step_link_or_add()
            data = {CONF_ADDRESS: address}
            if self._serial_number:
                data[CONF_SERIAL_NUMBER] = self._serial_number
                data[CONF_ENTITY_IDENTITY] = self.unique_id
            return self.async_create_entry(title=name, data=data)

        # Build dropdown of currently discovered devices
        devices = {
            info.address: info.name or info.address
            for info in bluetooth.async_discovered_service_info(self.hass)
            if _matches_nubert(info) or _maybe_nubert(info)
        }

        if devices:
            address_selector = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=addr, label=name)
                        for addr, name in devices.items()
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    custom_value=True,
                )
            )
            schema = vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): address_selector,
                    vol.Optional(CONF_NAME): str,
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): str,
                    vol.Optional(CONF_NAME): str,
                }
            )

        return self.async_show_form(step_id="user", data_schema=schema)

    async def _async_identify(
        self, address: str, *, discovery: bool = False
    ) -> ConfigFlowResult | None:
        """Probe a candidate before assigning its permanent config entry ID."""
        coordinator = NubertSpeakerCoordinator(self.hass, address, address)
        try:
            self._serial_number = await coordinator.async_read_serial_number()
        except Exception:
            _LOGGER.debug("Could not read serial number at %s", address, exc_info=True)
            return self.async_abort(reason="cannot_connect")
        finally:
            await coordinator.async_disconnect()

        self._candidate_address = address

        if self._serial_number is None:
            for entry in self._async_current_entries(include_ignore=False):
                if entry.data.get(CONF_ADDRESS) == address:
                    return self.async_abort(reason="already_configured")
            if discovery and self._async_current_entries(include_ignore=False):
                return self.async_abort(reason="serial_unavailable")

        unique_id = f"serial:{self._serial_number}" if self._serial_number else address
        await self.async_set_unique_id(unique_id)
        existing = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
        if existing is None:
            if self._serial_number:
                for entry in self._async_current_entries(include_ignore=False):
                    if (
                        entry.data.get(CONF_ADDRESS) == address
                        and not entry.data.get(CONF_SERIAL_NUMBER)
                    ):
                        await self._async_relink(entry)
                        return self.async_abort(reason="already_configured")
            return None
        if self._serial_number and existing.data.get(CONF_ADDRESS) != address:
            stored = self.hass.data.get(DOMAIN, {}).get(existing.entry_id, {})
            active = stored.get("coordinator")
            if active and active.connected:
                return self.async_abort(reason="serial_collision")
            self.hass.config_entries.async_update_entry(
                existing, data={**existing.data, CONF_ADDRESS: address}
            )
            await self.hass.config_entries.async_reload(existing.entry_id)
        return self.async_abort(reason="already_configured")

    def _legacy_entries(self) -> list[ConfigEntry]:
        """Old entries that might be the same speaker at a new address."""
        entries = []
        for entry in self._async_current_entries(include_ignore=False):
            stored = self.hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            active = stored.get("coordinator")
            if not entry.data.get(CONF_SERIAL_NUMBER) and not (
                active and active.connected
            ):
                entries.append(entry)
        return entries

    async def _async_relink(self, entry: ConfigEntry) -> None:
        assert self._candidate_address and self._serial_number
        self.hass.config_entries.async_update_entry(
            entry,
            unique_id=f"serial:{self._serial_number}",
            data={
                **entry.data,
                CONF_ADDRESS: self._candidate_address,
                CONF_SERIAL_NUMBER: self._serial_number,
                CONF_ENTITY_IDENTITY: entry.data.get(CONF_ENTITY_IDENTITY, entry.unique_id),
            },
        )
        await self.hass.config_entries.async_reload(entry.entry_id)

    async def async_step_link_or_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow a one-time match when an old address vanished before migration."""
        if user_input is not None:
            selected = user_input["existing_entry"]
            if selected != "new":
                entry = next(
                    (entry for entry in self._legacy_entries() if entry.entry_id == selected),
                    None,
                )
                if entry is None:
                    return self.async_abort(reason="speaker_unavailable")
                await self._async_relink(entry)
                return self.async_abort(reason="relinked")
            if self._discovery_info is not None:
                return await self.async_step_bluetooth_confirm()
            assert self._candidate_address is not None
            return self.async_create_entry(
                title=self._pending_name or self._candidate_address,
                data={
                    CONF_ADDRESS: self._candidate_address,
                    CONF_SERIAL_NUMBER: self._serial_number,
                    CONF_ENTITY_IDENTITY: self.unique_id,
                },
            )

        choices = [selector.SelectOptionDict(value="new", label="Add as a new speaker")]
        choices.extend(
            selector.SelectOptionDict(
                value=entry.entry_id,
                label=f"Relink {entry.title} ({entry.data[CONF_ADDRESS]})",
            )
            for entry in self._legacy_entries()
        )
        return self.async_show_form(
            step_id="link_or_add",
            data_schema=vol.Schema({
                vol.Required("existing_entry"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=choices, mode=selector.SelectSelectorMode.DROPDOWN
                    )
                )
            }),
        )


class NubertOptionsFlow(OptionsFlow):
    """Configure the listening range for a detected speaker."""

    def __init__(self, config_entry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        stored = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        coordinator = stored.get("coordinator")
        if coordinator is None:
            return self.async_abort(reason="speaker_unavailable")
        model = coordinator.model
        low, high = supported_db_range(model)
        default_min, default_max = default_db_range(model)
        options = self._entry.options
        errors = {}
        if user_input is not None:
            if valid_settings(
                user_input[CONF_MIN_VOLUME_DB], user_input[CONF_MAX_VOLUME_DB],
                user_input[CONF_VOLUME_GAMMA], model,
            ):
                return self.async_create_entry(title="", data=user_input)
            errors["base"] = "invalid_volume_range"

        db_selector = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=low, max=high, step=1, mode=selector.NumberSelectorMode.BOX
            )
        )
        gamma_selector = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.5, max=3.0, step=0.1, mode=selector.NumberSelectorMode.BOX
            )
        )
        schema = vol.Schema({
            vol.Required(
                CONF_MIN_VOLUME_DB, default=options.get(CONF_MIN_VOLUME_DB, default_min)
            ): db_selector,
            vol.Required(
                CONF_MAX_VOLUME_DB, default=options.get(CONF_MAX_VOLUME_DB, default_max)
            ): db_selector,
            vol.Required(
                CONF_VOLUME_GAMMA,
                default=options.get(CONF_VOLUME_GAMMA, DEFAULT_VOLUME_GAMMA),
            ): gamma_selector,
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)


def _matches_nubert(info: BluetoothServiceInfo) -> bool:
    """Return True if the advertisement matches a Nubert speaker."""
    return any(u.lower() in ADV_UUIDS for u in (info.service_uuids or []))


def _maybe_nubert(info: BluetoothServiceInfo) -> bool:
    """Return True for nuXero / nuConnect ampXL style advertisements."""
    return any(u.lower() in ADV_UUIDS_EXTRA for u in (info.service_uuids or []))
