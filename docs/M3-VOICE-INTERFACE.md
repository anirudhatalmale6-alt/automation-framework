# Milestone 3: Voice Interface Abstraction

## Overview

M3 provides a clean, provider-agnostic voice call interface. The abstraction allows integration with any voice provider (Twilio, Vonage, Plivo, custom SIP, etc.) without changing the core framework.

**Key Principle**: No provider lock-in. The framework provides the interface; users implement the provider.

## Architecture

```
src/voice/
├── __init__.py     # Module exports
├── interface.py    # Abstract interface + MockProvider
└── handlers.py     # Orchestrator integration
```

## Voice Interface

### Core Components

```python
from voice.interface import (
    VoiceProvider,      # Abstract base class
    VoiceCall,          # Call representation
    VoiceConfig,        # Provider configuration
    CallState,          # Call states enum
    CallEvent,          # Event from calls
    CallEventType,      # Event types enum
    MockVoiceProvider,  # Testing provider
)
```

### VoiceConfig

Configuration for any voice provider:

```python
config = VoiceConfig(
    provider_name="twilio",           # Provider identifier
    api_key="your_api_key",           # Provider API key
    api_secret="your_api_secret",     # Provider API secret
    account_id="your_account_id",     # Provider account ID
    default_timeout_seconds=30,       # Call timeout
    max_call_duration_seconds=3600,   # Max call length
    recording_enabled=False,          # Enable recording
    tts_voice="en-US-Standard-A",     # TTS voice
    tts_language="en-US",             # TTS language
    stt_language="en-US",             # STT language
    status_callback_url=None,         # Webhook URL
    extra={},                         # Provider-specific options
)
```

### CallState

```python
class CallState(Enum):
    IDLE = "idle"
    INITIATING = "initiating"
    RINGING = "ringing"
    CONNECTED = "connected"
    ON_HOLD = "on_hold"
    TRANSFERRING = "transferring"
    ENDED = "ended"
    FAILED = "failed"
```

### CallEventType

```python
class CallEventType(Enum):
    CALL_INITIATED = "call_initiated"
    CALL_RINGING = "call_ringing"
    CALL_ANSWERED = "call_answered"
    CALL_ENDED = "call_ended"
    CALL_FAILED = "call_failed"
    DTMF_RECEIVED = "dtmf_received"
    SPEECH_DETECTED = "speech_detected"
    RECORDING_STARTED = "recording_started"
    RECORDING_STOPPED = "recording_stopped"
    HOLD_STARTED = "hold_started"
    HOLD_ENDED = "hold_ended"
    TRANSFER_INITIATED = "transfer_initiated"
    TRANSFER_COMPLETED = "transfer_completed"
    ERROR = "error"
```

## Implementing a Provider

To integrate with a real voice provider, implement the `VoiceProvider` abstract class:

```python
from voice.interface import VoiceProvider, VoiceCall, VoiceConfig, CallState

class TwilioProvider(VoiceProvider):
    """Example Twilio implementation."""

    def __init__(self, config: VoiceConfig):
        super().__init__(config)
        # Initialize Twilio client
        from twilio.rest import Client
        self._client = Client(config.account_id, config.api_secret)

    async def initialize(self) -> None:
        """Initialize provider."""
        self._initialized = True

    async def shutdown(self) -> None:
        """Cleanup resources."""
        for call_id in list(self._active_calls.keys()):
            await self.end_call(call_id)
        self._initialized = False

    async def make_call(
        self,
        to_number: str,
        from_number: Optional[str] = None,
        **kwargs
    ) -> VoiceCall:
        """Make outbound call via Twilio."""
        call = self._client.calls.create(
            to=to_number,
            from_=from_number or self.config.extra.get("default_from"),
            url=kwargs.get("twiml_url"),
            status_callback=self.config.status_callback_url,
        )

        voice_call = VoiceCall(
            call_id=call.sid,
            state=CallState.INITIATING,
            from_number=from_number,
            to_number=to_number,
        )
        self._register_call(voice_call)
        return voice_call

    # ... implement other abstract methods
```

### Required Methods

| Method | Description |
|--------|-------------|
| `initialize()` | Initialize provider connection |
| `shutdown()` | Cleanup and end active calls |
| `make_call(to, from, **kwargs)` | Initiate outbound call |
| `answer_call(call_id)` | Answer incoming call |
| `end_call(call_id)` | End/hang up call |
| `hold_call(call_id, hold)` | Hold or resume call |
| `transfer_call(call_id, to)` | Transfer call |
| `play_audio(call_id, url/text)` | Play audio or TTS |
| `start_recording(call_id)` | Start recording |
| `stop_recording(call_id)` | Stop recording, get URL |
| `send_dtmf(call_id, digits)` | Send DTMF tones |
| `get_call_status(call_id)` | Get call status |

## Mock Provider

For testing without a real provider:

```python
from voice.interface import MockVoiceProvider, VoiceConfig

config = VoiceConfig(provider_name="mock")
provider = MockVoiceProvider(config)

await provider.initialize()

# Make a simulated call
call = await provider.make_call("+1234567890")
print(f"Call ID: {call.call_id}")
print(f"State: {call.state}")

# Call automatically progresses: INITIATING -> RINGING -> CONNECTED
await asyncio.sleep(3)
print(f"State: {call.state}")  # CONNECTED

# End call
await provider.end_call(call.call_id)
```

## Workflow Integration

### Voice Actions

Register voice actions with the orchestrator:

| Action | Description | Params |
|--------|-------------|--------|
| `voice_make_call` | Make outbound call | `to_number`, `from_number` |
| `voice_end_call` | End call | `call_id` |
| `voice_play_audio` | Play audio file | `call_id`, `audio_url` |
| `voice_play_tts` | Play text-to-speech | `call_id`, `text`, `voice` |
| `voice_hold` | Hold/resume call | `call_id`, `hold` |
| `voice_transfer` | Transfer call | `call_id`, `to_number` |
| `voice_send_dtmf` | Send DTMF tones | `call_id`, `digits` |
| `voice_start_recording` | Start recording | `call_id` |
| `voice_stop_recording` | Stop recording | `call_id` |
| `voice_get_status` | Get call status | `call_id` (optional) |

### Workflow Example

```yaml
id: outbound_call_workflow
name: Outbound Call Workflow
trigger_command: /call

steps:
  - type: voice_make_call
    params:
      to_number: "{{trigger.phone}}"
      from_number: "+1800555000"
    output_key: call_result

  - type: delay
    params:
      seconds: 5

  - type: voice_play_tts
    params:
      call_id: "{{call_result.call_id}}"
      text: "Hello, this is an automated message."

  - type: delay
    params:
      seconds: 10

  - type: voice_end_call
    params:
      call_id: "{{call_result.call_id}}"
```

## Environment Configuration

```yaml
# docker-compose.yml
environment:
  # Voice provider type ("mock" for testing)
  - VOICE_PROVIDER=mock

  # Provider credentials (for real providers)
  - VOICE_API_KEY=your_api_key
  - VOICE_API_SECRET=your_api_secret
  - VOICE_ACCOUNT_ID=your_account_id

  # Call settings
  - VOICE_TIMEOUT=30
  - VOICE_RECORDING=false

  # TTS settings
  - VOICE_TTS_VOICE=en-US-Standard-A
  - VOICE_TTS_LANGUAGE=en-US
```

## Event Handling

Voice events are routed to the rules engine:

```python
# Event handler receives CallEvent
async def handle_voice_event(event: CallEvent):
    print(f"Event: {event.event_type}")
    print(f"Call: {event.call_id}")
    print(f"Data: {event.data}")

provider.add_event_handler(handle_voice_event)
```

### Trigger Rules

Voice events can trigger rules:

```yaml
id: call_ended_notification
name: Notify on Call End
trigger:
  type: voice_event
  event_type: call_ended

conditions:
  - field: duration
    operator: gt
    value: 60

actions:
  - type: log
    params:
      message: "Long call ended: {{trigger.call_id}}"
```

## Testing

### Test with Mock Provider

```bash
# Enable mock voice provider
export VOICE_PROVIDER=mock

# Start framework
docker-compose up --build

# The mock provider simulates calls without real telephony
```

### Test via Telegram

```
/run call --phone=+1234567890
```

The mock provider will log simulated call events.

## Provider Implementation Guide

1. **Create provider class** extending `VoiceProvider`
2. **Implement all abstract methods**
3. **Register in main.py** (add elif for your provider type)
4. **Handle webhooks** if provider uses callbacks

### Webhook Handling

For providers that use webhooks (Twilio, Vonage):

```python
# Add webhook endpoint to health server
async def _voice_webhook(self, request: web.Request) -> web.Response:
    data = await request.json()

    # Parse provider-specific webhook format
    event = parse_webhook_event(data)

    # Emit event to handlers
    await self.voice_provider._emit_event(event)

    return web.Response(text="OK")
```

## Summary

The voice interface provides:

- **Provider-agnostic abstraction** - Switch providers without code changes
- **Full call control** - Make, answer, hold, transfer, end calls
- **Media capabilities** - Play audio, TTS, recording
- **Event system** - React to call events in rules/workflows
- **Mock provider** - Test without real telephony costs
- **Workflow integration** - Voice actions in YAML workflows
