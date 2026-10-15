from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (  # type: ignore
    BluetoothServiceInfo,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
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
)
from .volume import default_db_range, supported_db_range, valid_settings

_LOGGER = logging.getLogger(__name__)


class NubertConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nubert BLE speakers."""

    VERSION = 1

    _discovery_info: BluetoothServiceInfo | None = None

    @staticmethod
    def async_get_options_flow(config_entry) -> NubertOptionsFlow:
        return NubertOptionsFlow(config_entry)

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfo
    ) -> ConfigFlowResult:
        """Handle a flow initialized by bluetooth discovery."""

        if not _matches_nubert(discovery_info):
            return self.async_abort(reason="not_nubert")

        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }

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
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            data = {CONF_ADDRESS: address}
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
