"""Async client for OpenClaw."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import USER_AGENT


class OpenClawError(Exception):
    """Base exception for OpenClaw failures."""


class OpenClawConnectionError(OpenClawError):
    """Raised when OpenClaw cannot be reached."""


class OpenClawAuthError(OpenClawError):
    """Raised when OpenClaw rejects authentication."""


class OpenClawRequestError(OpenClawError):
    """Raised when OpenClaw returns an invalid response."""


@dataclass(slots=True)
class OpenClawServiceAction:
    """Structured Home Assistant service call requested by OpenClaw."""

    domain: str
    service: str
    target: dict[str, Any]
    data: dict[str, Any]


@dataclass(slots=True)
class OpenClawAssistantReply:
    """Parsed assistant reply content."""

    speech: str
    action: OpenClawServiceAction | None = None


@dataclass(slots=True)
class OpenClawRequestContext:
    """Stable request settings for a Home Assistant config entry."""

    agent_id: str
    stable_session_id: str
    entity_context_enabled: bool


class OpenClawClient:
    """Thin HTTP client for future OpenClaw conversation requests."""

    def __init__(
        self,
        hass: HomeAssistant,
        base_url: str,
        auth_token: str,
        agent_id: str,
        stable_session_id: str,
        entity_context_enabled: bool,
    ) -> None:
        """Initialize the client."""
        self.hass = hass
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.context = OpenClawRequestContext(
            agent_id=agent_id,
            stable_session_id=stable_session_id,
            entity_context_enabled=entity_context_enabled,
        )
        self._session = async_get_clientsession(hass)

    @property
    def endpoint_url(self) -> str:
        """Return the chat completions endpoint URL."""
        return f"{self.base_url}/v1/chat/completions"

    def build_headers(
        self,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, str]:
        """Return request headers."""
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "x-openclaw-agent-id": agent_id or self.context.agent_id,
            "x-openclaw-session-key": session_id or self.context.stable_session_id,
        }

    async def async_probe(self) -> None:
        """Perform a lightweight connectivity probe.

        This avoids assuming a finalized OpenClaw API route. A successful HTTP
        response from the configured base URL is treated as reachable.
        """
        try:
            async with self._session.get(
                self.base_url,
                headers=self.build_headers(),
            ) as resp:
                if resp.status >= 400:
                    raise OpenClawConnectionError(
                        f"OpenClaw probe failed with status {resp.status}"
                    )
        except ClientError as err:
            raise OpenClawConnectionError(str(err)) from err

    async def async_validate_connection(self) -> None:
        """Validate the real chat-completions path with a minimal request."""
        await self.async_converse(
            prompt="Reply with OK.",
            entity_context=None,
            agent_id=self.context.agent_id,
            session_id="ha-openclaw-validation",
            max_tokens=8,
        )

    def build_payload(
        self,
        prompt: str,
        entity_context: list[dict[str, Any]] | None,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Build the canonical request payload for future conversation calls."""
        messages: list[dict[str, Any]] = []
        if self.context.entity_context_enabled and entity_context:
            messages.append(
                {
                    "role": "system",
                    "content": self._format_entity_context(entity_context),
                }
            )
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": f"openclaw:{agent_id or self.context.agent_id}",
            "messages": messages,
            "stream": False,
            "user": session_id or self.context.stable_session_id,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return payload

    async def async_converse(
        self,
        prompt: str,
        entity_context: list[dict[str, Any]] | None = None,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Send a chat completion request to OpenClaw."""
        payload = self.build_payload(
            prompt=prompt,
            entity_context=entity_context,
            agent_id=agent_id,
            session_id=session_id,
            max_tokens=max_tokens,
        )
        try:
            async with self._session.post(
                self.endpoint_url,
                headers=self.build_headers(agent_id=agent_id, session_id=session_id),
                json=payload,
            ) as resp:
                if resp.status in (401, 403):
                    detail = await resp.text()
                    raise OpenClawAuthError(detail or "authentication failed")
                if resp.status >= 400:
                    detail = await resp.text()
                    raise OpenClawRequestError(
                        f"OpenClaw request failed with status {resp.status}: {detail}"
                    )
                data = await resp.json()
        except ClientError as err:
            raise OpenClawConnectionError(str(err)) from err

        if not isinstance(data, dict):
            raise OpenClawRequestError("OpenClaw returned a non-object response")

        return data

    def extract_response_text(self, response: dict[str, Any]) -> str:
        """Extract assistant text from an OpenAI-compatible chat response."""
        return self.parse_assistant_reply(response).speech

    def parse_assistant_reply(
        self, response: dict[str, Any]
    ) -> OpenClawAssistantReply:
        """Parse assistant speech and an optional structured action contract."""
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenClawRequestError("OpenClaw response did not include choices")

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise OpenClawRequestError("OpenClaw response choice was not an object")

        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise OpenClawRequestError("OpenClaw response did not include a message")

        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return self._parse_message_content(content.strip())

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            if parts:
                return self._parse_message_content("\n".join(parts))

        raise OpenClawRequestError("OpenClaw response did not include assistant text")

    def _format_entity_context(self, entity_context: list[dict[str, Any]]) -> str:
        """Render a compact entity context message."""
        lines = [
            "Home Assistant entity context. Keep answers grounded in these current states.",
        ]
        for item in entity_context:
            entity_id = item.get("entity_id", "unknown")
            name = item.get("name") or entity_id
            state = item.get("state", "unknown")
            lines.append(f"- {name} ({entity_id}): {state}")
            attributes = item.get("attributes")
            if isinstance(attributes, dict) and attributes:
                formatted = ", ".join(
                    f"{key}={value}" for key, value in attributes.items()
                )
                lines.append(f"  attributes: {formatted}")
        return "\n".join(lines)

    def _parse_message_content(self, content: str) -> OpenClawAssistantReply:
        """Parse plain text or the conservative JSON action contract."""
        payload = self._try_parse_json_contract(content)
        if payload is None:
            return OpenClawAssistantReply(speech=content)

        speech = payload.get("speech")
        if not isinstance(speech, str) or not speech.strip():
            raise OpenClawRequestError(
                "Structured OpenClaw response must include non-empty speech"
            )

        action_payload = payload.get("action")
        if action_payload is None:
            return OpenClawAssistantReply(speech=speech.strip())

        if not isinstance(action_payload, dict):
            raise OpenClawRequestError("Structured action must be an object")

        domain = action_payload.get("domain")
        service = action_payload.get("service")
        target = action_payload.get("target", {})
        data = action_payload.get("data", {})

        if not isinstance(domain, str) or not domain.strip():
            raise OpenClawRequestError("Structured action requires a domain")
        if not isinstance(service, str) or not service.strip():
            raise OpenClawRequestError("Structured action requires a service")
        if not isinstance(target, dict):
            raise OpenClawRequestError("Structured action target must be an object")
        if not isinstance(data, dict):
            raise OpenClawRequestError("Structured action data must be an object")

        return OpenClawAssistantReply(
            speech=speech.strip(),
            action=OpenClawServiceAction(
                domain=domain.strip(),
                service=service.strip(),
                target=target,
                data=data,
            ),
        )

    def _try_parse_json_contract(self, content: str) -> dict[str, Any] | None:
        """Parse a JSON object from plain or fenced content."""
        candidate = content.strip()
        if candidate.startswith("```json") and candidate.endswith("```"):
            candidate = candidate[7:-3].strip()
        elif candidate.startswith("```") and candidate.endswith("```"):
            candidate = candidate[3:-3].strip()

        if not candidate.startswith("{"):
            return None

        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            return None

        if not isinstance(payload, dict):
            raise OpenClawRequestError("Structured OpenClaw response must be an object")
        return payload
