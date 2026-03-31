"""Conversation platform for OpenClaw."""

from __future__ import annotations

from dataclasses import dataclass
import re
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
MATCH_STOPWORDS = {
    "a",
    "an",
    "any",
    "current",
    "for",
    "get",
    "give",
    "how",
    "i",
    "is",
    "me",
    "of",
    "show",
    "state",
    "status",
    "temp",
    "temperature",
    "tell",
    "the",
    "to",
    "value",
    "what",
    "whats",
    "which",
}
GENERIC_WEATHER_PATTERNS = (
    re.compile(r"\bweather\b"),
    re.compile(r"\bforecast\b"),
    re.compile(r"\btemperature\b"),
    re.compile(r"\btemp\b"),
    re.compile(r"\bhumid(?:ity)?\b"),
    re.compile(r"\bwind(?:y)?\b"),
    re.compile(r"\brain(?:ing)?\b"),
    re.compile(r"\bsnow(?:ing)?\b"),
    re.compile(r"\boutside\b"),
)
EXPLICIT_LOCATION_PATTERN = re.compile(
    r"\b(?:in|for|at|near|around|outside of)\s+"
    r"(?:[A-Z][a-z]+(?:[\s-][A-Z][a-z]+)*|\d{5}(?:-\d{4})?)\b"
)


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
            text=self._inject_home_location_for_generic_weather(user_input.text),
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
        """Collect the best matching entity states from the utterance."""
        if not self._client.context.entity_context_enabled:
            return None

        utterance = user_input.text.lower()
        utterance_tokens = self._tokenize_for_matching(utterance)
        ranked: list[tuple[int, dict[str, Any]]] = []

        for state in self.hass.states.async_all():
            score = self._score_entity_match(state, utterance, utterance_tokens)
            if score <= 0:
                continue

            ranked.append(
                (
                    score,
                    {
                        "entity_id": state.entity_id,
                        "name": state.attributes.get("friendly_name"),
                        "state": state.state,
                        "attributes": self._compact_attributes(state.attributes),
                    },
                )
            )

        if not ranked:
            return None

        ranked.sort(
            key=lambda item: (
                -item[0],
                str(item[1].get("name") or item[1]["entity_id"]).lower(),
            )
        )
        return [item[1] for item in ranked[:MAX_ENTITY_CONTEXT_ENTRIES]]

    def _inject_home_location_for_generic_weather(self, utterance: str) -> str:
        """Default generic weather questions to the configured home location."""
        if not self._is_generic_weather_request(utterance):
            return utterance

        home_location = self._home_location_label()
        if home_location is None:
            return utterance

        return (
            f"{utterance.rstrip()} "
            f"(Use the configured Home Assistant home location: {home_location}.)"
        )

    def _score_entity_match(
        self,
        state,
        utterance: str,
        utterance_tokens: set[str],
    ) -> int:
        """Return a score for how well a state matches the utterance."""
        entity_id = state.entity_id.lower()
        friendly_name = str(
            state.attributes.get("friendly_name", state.entity_id)
        ).lower()
        entity_tokens = self._tokenize_for_matching(entity_id)
        name_tokens = self._tokenize_for_matching(friendly_name)
        candidate_tokens = entity_tokens | name_tokens

        if friendly_name and friendly_name in utterance:
            return 300 + len(name_tokens)
        if entity_id in utterance:
            return 280 + len(entity_tokens)

        if candidate_tokens and candidate_tokens.issubset(utterance_tokens):
            return 220 + len(candidate_tokens)

        overlap = candidate_tokens & utterance_tokens
        if len(overlap) >= 2:
            return 150 + len(overlap)
        if len(overlap) == 1 and any(len(token) >= 4 for token in overlap):
            return 100
        return 0

    def _tokenize_for_matching(self, text: str) -> set[str]:
        """Tokenize strings for lightweight entity matching."""
        tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", text.lower())
            if len(token) >= 2 and token not in MATCH_STOPWORDS
        }
        return tokens

    def _is_generic_weather_request(self, utterance: str) -> bool:
        """Return True when the user asked about weather without a location."""
        normalized = utterance.strip()
        if not normalized:
            return False

        lowered = normalized.lower()
        if not any(pattern.search(lowered) for pattern in GENERIC_WEATHER_PATTERNS):
            return False

        if EXPLICIT_LOCATION_PATTERN.search(normalized):
            return False

        return True

    def _home_location_label(self) -> str | None:
        """Return the configured Home Assistant home location label."""
        location_name = self.hass.config.location_name.strip()
        if not location_name:
            return None

        country = self.hass.config.country
        if country:
            return f"{location_name}, {country}"
        return location_name

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
