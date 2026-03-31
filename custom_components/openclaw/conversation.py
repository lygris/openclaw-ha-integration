"""Conversation platform for OpenClaw."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OpenClawConfigEntry
from .client import (
    OpenClawAssistantReply,
    OpenClawAuthError,
    OpenClawConnectionError,
    OpenClawRequestError,
    OpenClawServiceAction,
)

MAX_ENTITY_CONTEXT_ENTRIES = 5


@dataclass(slots=True)
class OpenClawConversationRequest:
    """Internal request model for OpenClaw conversation handling."""

    text: str
    entity_context: list[dict[str, Any]] | None = None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the OpenClaw conversation entity."""
    async_add_entities([OpenClawConversationEntity(entry)])


class OpenClawConversationEntity(conversation.ConversationEntity):
    """Home Assistant conversation entity backed by OpenClaw."""

    _attr_has_entity_name = True
    _attr_name = "Assistant"

    def __init__(self, entry: OpenClawConfigEntry) -> None:
        """Initialize the conversation entity."""
        self.entry = entry
        self._client = entry.runtime_data.client
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return supported languages."""
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Send the user utterance to OpenClaw and return an Assist result."""
        request = OpenClawConversationRequest(
            text=user_input.text,
            entity_context=self._async_collect_entity_context(user_input),
        )
        response = intent.IntentResponse(language=user_input.language)

        try:
            raw_response = await self._client.async_converse(
                prompt=request.text,
                entity_context=request.entity_context,
            )
            assistant_reply = self._client.parse_assistant_reply(raw_response)
        except OpenClawAuthError:
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "OpenClaw authentication failed",
            )
            return conversation.ConversationResult(
                response=response,
                conversation_id=user_input.conversation_id
                or self._client.context.stable_session_id,
            )
        except OpenClawConnectionError:
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "OpenClaw is unreachable",
            )
            return conversation.ConversationResult(
                response=response,
                conversation_id=user_input.conversation_id
                or self._client.context.stable_session_id,
            )
        except OpenClawRequestError as err:
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                str(err),
            )
            return conversation.ConversationResult(
                response=response,
                conversation_id=user_input.conversation_id
                or self._client.context.stable_session_id,
            )

        if assistant_reply.action is not None:
            try:
                await self._async_execute_action(
                    assistant_reply=assistant_reply,
                    matched_entities=request.entity_context or [],
                )
            except OpenClawRequestError as err:
                response.async_set_error(
                    intent.IntentResponseErrorCode.UNKNOWN,
                    str(err),
                )
                return conversation.ConversationResult(
                    response=response,
                    conversation_id=user_input.conversation_id
                    or self._client.context.stable_session_id,
                )

        chat_log.async_trace(
            {
                "openclaw": {
                    "agent_id": self._client.context.agent_id,
                    "session_id": self._client.context.stable_session_id,
                    "entity_context_count": len(request.entity_context or []),
                    "action_requested": assistant_reply.action is not None,
                }
            }
        )
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id=self.entity_id,
                content=assistant_reply.speech,
            )
        )
        response.async_set_speech(assistant_reply.speech)
        return conversation.ConversationResult(
            response=response,
            conversation_id=user_input.conversation_id
            or self._client.context.stable_session_id,
            continue_conversation=chat_log.continue_conversation,
        )

    def _async_collect_entity_context(
        self, user_input: conversation.ConversationInput
    ) -> list[dict[str, Any]] | None:
        """Collect a compact set of matching entity states from the utterance."""
        if not self._client.context.entity_context_enabled:
            return None

        utterance = user_input.text.lower()
        matches: list[dict[str, Any]] = []
        for state in self.hass.states.async_all():
            if len(matches) >= MAX_ENTITY_CONTEXT_ENTRIES:
                break

            entity_id = state.entity_id.lower()
            friendly_name = str(
                state.attributes.get("friendly_name", state.entity_id)
            ).lower()

            if entity_id not in utterance and friendly_name not in utterance:
                continue

            matches.append(
                {
                    "entity_id": state.entity_id,
                    "name": state.attributes.get("friendly_name"),
                    "state": state.state,
                    "attributes": self._compact_attributes(state.attributes),
                }
            )

        return matches or None

    async def _async_execute_action(
        self,
        *,
        assistant_reply: OpenClawAssistantReply,
        matched_entities: list[dict[str, Any]],
    ) -> None:
        """Execute the conservative structured action contract."""
        action = assistant_reply.action
        if action is None:
            return

        if not self.hass.services.has_service(action.domain, action.service):
            raise OpenClawRequestError(
                f"Home Assistant service {action.domain}.{action.service} is unavailable"
            )

        allowed_entities = {
            item["entity_id"] for item in matched_entities if "entity_id" in item
        }
        target_entity_ids = self._normalize_entity_targets(action)
        if not target_entity_ids:
            raise OpenClawRequestError(
                "Structured action must target at least one matched entity_id"
            )
        if not target_entity_ids.issubset(allowed_entities):
            raise OpenClawRequestError(
                "Structured action target must be limited to explicitly matched entities"
            )

        await self.hass.services.async_call(
            action.domain,
            action.service,
            service_data=action.data,
            target=action.target,
            blocking=True,
        )

    def _normalize_entity_targets(self, action: OpenClawServiceAction) -> set[str]:
        """Normalize target entity ids from a structured action."""
        entity_ids = action.target.get("entity_id")
        if isinstance(entity_ids, str):
            return {entity_ids}
        if isinstance(entity_ids, list) and all(
            isinstance(item, str) for item in entity_ids
        ):
            return set(entity_ids)
        return set()

    def _compact_attributes(self, attributes: dict[str, Any]) -> dict[str, Any]:
        """Keep only small scalar attributes for prompt context."""
        compact: dict[str, Any] = {}
        for key, value in attributes.items():
            if key in {"friendly_name", "icon"}:
                continue
            if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 60:
                compact[key] = value
            if len(compact) >= 6:
                break
        return compact
