"""The OpenClaw integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_BASE_URL, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform

from .client import OpenClawClient
from .const import (
    CONF_AGENT_ID,
    CONF_ENTITY_CONTEXT_ENABLED,
    CONF_STABLE_SESSION_ID,
    DEFAULT_AGENT_ID,
    DEFAULT_ENTITY_CONTEXT_ENABLED,
)

OpenClawConfigEntry = ConfigEntry["OpenClawRuntimeData"]
PLATFORMS: tuple[Platform, ...] = (Platform.CONVERSATION,)


@dataclass(slots=True)
class OpenClawRuntimeData:
    """Runtime state stored on a config entry."""

    client: OpenClawClient


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the OpenClaw integration via YAML."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OpenClawConfigEntry) -> bool:
    """Set up OpenClaw from a config entry."""
    client = OpenClawClient(
        hass=hass,
        base_url=entry.data[CONF_BASE_URL],
        auth_token=entry.data[CONF_TOKEN],
        agent_id=entry.data.get(CONF_AGENT_ID, DEFAULT_AGENT_ID),
        stable_session_id=entry.data[CONF_STABLE_SESSION_ID],
        entity_context_enabled=entry.data.get(
            CONF_ENTITY_CONTEXT_ENABLED, DEFAULT_ENTITY_CONTEXT_ENABLED
        ),
    )
    entry.runtime_data = OpenClawRuntimeData(client=client)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OpenClawConfigEntry) -> bool:
    """Unload an OpenClaw config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
