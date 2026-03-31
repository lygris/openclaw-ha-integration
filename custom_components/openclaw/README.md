# OpenClaw Home Assistant Integration

Initial scaffold for a custom Home Assistant integration that stores the
minimum OpenClaw configuration needed for a future conversation agent.

## What This Version Supports

- Config entry setup from the Home Assistant UI
- Stored fields for:
  - OpenClaw base URL
  - auth token
  - target agent id, defaulting to `ha-assist`
  - stable session id
  - optional entity-context behavior
- Real Home Assistant `conversation` platform entity
- Real OpenClaw gateway calls to `/v1/chat/completions`
- Exact live entity state from Home Assistant for explicit utterance matches only
- Conservative optional action bridge for one structured Home Assistant service call
- Config-flow validation against the real chat-completions endpoint
- Unique config entries per `base_url + agent_id + stable_session_id`

## Intentional Gaps

- Streaming responses are not implemented yet
- Entity matching is intentionally simple and capped to avoid large prompt stuffing
- Config validation still performs a real model call, just a very small one
- Structured actions are limited to one explicit service call whose target entity ids must already be among the matched entities

## Structured Action Contract

Plain text responses still work. To request an action, OpenClaw must return a
JSON object as the assistant message content:

```json
{
  "speech": "Turning on the kitchen lamp.",
  "action": {
    "domain": "light",
    "service": "turn_on",
    "target": {
      "entity_id": "light.kitchen_lamp"
    },
    "data": {
      "brightness_pct": 50
    }
  }
}
```

This integration only executes the action when:

- the service exists in Home Assistant
- `target.entity_id` is present
- every target entity id was explicitly matched from the user utterance

## Assumptions

- The most appropriate local target path is this Home Assistant config repo's
  `custom_components/openclaw` directory.
- OpenClaw is reachable at a user-provided base URL, and the local deployment
  currently exposes port `18789`.
- The OpenClaw gateway chat-completions endpoint is enabled and available at
  `/v1/chat/completions`.
- A stable session id should be user-managed so Home Assistant can keep
  continuity with one OpenClaw session over time through OpenClaw's session key.
