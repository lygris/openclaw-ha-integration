"""Config flow for OpenClaw."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_TOKEN
from homeassistant.data_entry_flow import FlowResult

from .client import (
    OpenClawAuthError,
    OpenClawClient,
    OpenClawConnectionError,
    OpenClawRequestError,
)
from .const import (
    CONF_AGENT_ID,
    CONF_BASE_URL,
    CONF_ENTITY_CONTEXT_ENABLED,
    CONF_STABLE_SESSION_ID,
    DEFAULT_AGENT_ID,
    DEFAULT_ENTITY_CONTEXT_ENABLED,
    DOMAIN,
    NAME,
)


def _normalize_base_url(value: str) -> str:
    """Normalize and validate a base URL."""
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise vol.Invalid("invalid_base_url")
    return url


def _validate_non_empty(value: str) -> str:
    """Ensure a string field is not empty."""
    text = value.strip()
    if not text:
        raise vol.Invalid("required")
    return text


class OpenClawConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle an OpenClaw config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}

        if user_input is not None:
            normalized = {
                CONF_BASE_URL: _normalize_base_url(user_input[CONF_BASE_URL]),
                CONF_TOKEN: _validate_non_empty(user_input[CONF_TOKEN]),
                CONF_AGENT_ID: _validate_non_empty(user_input[CONF_AGENT_ID]),
                CONF_STABLE_SESSION_ID: _validate_non_empty(
                    user_input[CONF_STABLE_SESSION_ID]
                ),
                CONF_ENTITY_CONTEXT_ENABLED: user_input[
                    CONF_ENTITY_CONTEXT_ENABLED
                ],
            }

            unique_id = "|".join(
                [
                    normalized[CONF_BASE_URL],
                    normalized[CONF_AGENT_ID],
                    normalized[CONF_STABLE_SESSION_ID],
                ]
            )
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            client = OpenClawClient(
                hass=self.hass,
                base_url=normalized[CONF_BASE_URL],
                auth_token=normalized[CONF_TOKEN],
                agent_id=normalized[CONF_AGENT_ID],
                stable_session_id=normalized[CONF_STABLE_SESSION_ID],
                entity_context_enabled=normalized[CONF_ENTITY_CONTEXT_ENABLED],
            )

            try:
                await client.async_validate_connection()
            except OpenClawAuthError:
                errors["base"] = "invalid_auth"
            except OpenClawConnectionError:
                errors["base"] = "cannot_connect"
            except OpenClawRequestError:
                errors["base"] = "chat_endpoint_failed"
            else:
                title = (
                    f"{NAME} "
                    f"({urlparse(normalized[CONF_BASE_URL]).netloc}/"
                    f"{normalized[CONF_AGENT_ID]})"
                )
                return self.async_create_entry(title=title, data=normalized)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BASE_URL): str,
                    vol.Required(CONF_TOKEN): str,
                    vol.Required(CONF_AGENT_ID, default=DEFAULT_AGENT_ID): str,
                    vol.Required(CONF_STABLE_SESSION_ID): str,
                    vol.Required(
                        CONF_ENTITY_CONTEXT_ENABLED,
                        default=DEFAULT_ENTITY_CONTEXT_ENABLED,
                    ): bool,
                }
            ),
            errors=errors,
        )
