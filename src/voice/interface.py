"""
Voice Interface - Provider-agnostic abstraction for voice calls.

This module provides a clean interface for voice call functionality
without locking into any specific provider (Twilio, Vonage, etc.).

Implementation of actual providers is left to the user.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Callable, Awaitable, List, Dict
from datetime import datetime
import structlog

logger = structlog.get_logger()


class CallState(Enum):
    """Voice call states."""
    IDLE = "idle"
    INITIATING = "initiating"
    RINGING = "ringing"
    CONNECTED = "connected"
    ON_HOLD = "on_hold"
    TRANSFERRING = "transferring"
    ENDED = "ended"
    FAILED = "failed"


class CallEventType(Enum):
    """Types of call events."""
    CALL_INITIATED = "call_initiated"
    CALL_RINGING = "call_ringing"
    CALL_ANSWERED = "call_answered"
    CALL_ENDED = "call_ended"
    CALL_FAILED = "call_failed"
    DTMF_RECEIVED = "dtmf_received"
    SPEECH_DETECTED = "speech_detected"
    SPEECH_ENDED = "speech_ended"
    RECORDING_STARTED = "recording_started"
    RECORDING_STOPPED = "recording_stopped"
    HOLD_STARTED = "hold_started"
    HOLD_ENDED = "hold_ended"
    TRANSFER_INITIATED = "transfer_initiated"
    TRANSFER_COMPLETED = "transfer_completed"
    ERROR = "error"


@dataclass
class CallEvent:
    """Event from a voice call."""
    event_type: CallEventType
    call_id: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "event_type": self.event_type.value,
            "call_id": self.call_id,
            "timestamp": self.timestamp.isoformat(),
            "data": self.data,
        }


@dataclass
class VoiceConfig:
    """Configuration for voice provider."""
    # Provider identification
    provider_name: str = "generic"

    # Connection settings
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    account_id: Optional[str] = None

    # Call settings
    default_timeout_seconds: int = 30
    max_call_duration_seconds: int = 3600
    recording_enabled: bool = False

    # TTS/STT settings (if supported)
    tts_voice: Optional[str] = None
    tts_language: str = "en-US"
    stt_language: str = "en-US"

    # Callback URLs (for webhook-based providers)
    status_callback_url: Optional[str] = None
    event_callback_url: Optional[str] = None

    # Custom provider-specific settings
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VoiceCall:
    """Represents an active or completed voice call."""
    call_id: str
    state: CallState = CallState.IDLE
    from_number: Optional[str] = None
    to_number: Optional[str] = None
    direction: str = "outbound"  # "outbound" or "inbound"
    started_at: Optional[datetime] = None
    answered_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_seconds: int = 0
    recording_url: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "call_id": self.call_id,
            "state": self.state.value,
            "from_number": self.from_number,
            "to_number": self.to_number,
            "direction": self.direction,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "answered_at": self.answered_at.isoformat() if self.answered_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": self.duration_seconds,
            "recording_url": self.recording_url,
            "metadata": self.metadata,
        }


class VoiceProvider(ABC):
    """
    Abstract base class for voice providers.

    Implement this interface to integrate with any voice provider
    (Twilio, Vonage, Plivo, custom SIP, etc.).

    Example implementation:

    ```python
    class TwilioProvider(VoiceProvider):
        def __init__(self, config: VoiceConfig):
            super().__init__(config)
            from twilio.rest import Client
            self._client = Client(config.account_id, config.api_secret)

        async def make_call(self, to_number: str, from_number: str, **kwargs) -> VoiceCall:
            call = self._client.calls.create(
                to=to_number,
                from_=from_number,
                url=kwargs.get('twiml_url'),
            )
            return VoiceCall(
                call_id=call.sid,
                state=CallState.INITIATING,
                from_number=from_number,
                to_number=to_number,
            )
    ```
    """

    def __init__(self, config: VoiceConfig):
        """
        Initialize the voice provider.

        Args:
            config: Provider configuration
        """
        self.config = config
        self._event_handlers: List[Callable[[CallEvent], Awaitable[None]]] = []
        self._active_calls: Dict[str, VoiceCall] = {}
        self._initialized = False

    @abstractmethod
    async def initialize(self) -> None:
        """
        Initialize the provider connection.

        Called once before any calls are made.
        Implement provider-specific initialization here.
        """
        pass

    @abstractmethod
    async def shutdown(self) -> None:
        """
        Shutdown the provider and cleanup resources.

        End any active calls and release connections.
        """
        pass

    @abstractmethod
    async def make_call(
        self,
        to_number: str,
        from_number: Optional[str] = None,
        **kwargs: Any,
    ) -> VoiceCall:
        """
        Initiate an outbound call.

        Args:
            to_number: Destination phone number (E.164 format recommended)
            from_number: Caller ID (provider default if not specified)
            **kwargs: Provider-specific options

        Returns:
            VoiceCall object representing the call
        """
        pass

    @abstractmethod
    async def answer_call(self, call_id: str, **kwargs: Any) -> bool:
        """
        Answer an incoming call.

        Args:
            call_id: Call identifier
            **kwargs: Provider-specific options

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    async def end_call(self, call_id: str, **kwargs: Any) -> bool:
        """
        End/hang up a call.

        Args:
            call_id: Call identifier
            **kwargs: Provider-specific options

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    async def hold_call(self, call_id: str, hold: bool = True, **kwargs: Any) -> bool:
        """
        Put a call on hold or resume.

        Args:
            call_id: Call identifier
            hold: True to hold, False to resume
            **kwargs: Provider-specific options

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    async def transfer_call(
        self,
        call_id: str,
        to_number: str,
        **kwargs: Any,
    ) -> bool:
        """
        Transfer a call to another number.

        Args:
            call_id: Call identifier
            to_number: Transfer destination
            **kwargs: Provider-specific options

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    async def play_audio(
        self,
        call_id: str,
        audio_url: Optional[str] = None,
        text: Optional[str] = None,
        **kwargs: Any,
    ) -> bool:
        """
        Play audio or text-to-speech on a call.

        Args:
            call_id: Call identifier
            audio_url: URL of audio file to play
            text: Text to convert to speech (TTS)
            **kwargs: Provider-specific options (voice, language, etc.)

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    async def start_recording(self, call_id: str, **kwargs: Any) -> bool:
        """
        Start recording a call.

        Args:
            call_id: Call identifier
            **kwargs: Provider-specific options

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    async def stop_recording(self, call_id: str, **kwargs: Any) -> Optional[str]:
        """
        Stop recording and get recording URL.

        Args:
            call_id: Call identifier
            **kwargs: Provider-specific options

        Returns:
            Recording URL or None
        """
        pass

    @abstractmethod
    async def send_dtmf(self, call_id: str, digits: str, **kwargs: Any) -> bool:
        """
        Send DTMF tones on a call.

        Args:
            call_id: Call identifier
            digits: DTMF digits to send (0-9, *, #)
            **kwargs: Provider-specific options

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    async def get_call_status(self, call_id: str) -> Optional[VoiceCall]:
        """
        Get current status of a call.

        Args:
            call_id: Call identifier

        Returns:
            VoiceCall object or None if not found
        """
        pass

    # ==================== Event Handling ====================

    def add_event_handler(
        self,
        handler: Callable[[CallEvent], Awaitable[None]],
    ) -> None:
        """
        Register an event handler for call events.

        Args:
            handler: Async function that receives CallEvent
        """
        self._event_handlers.append(handler)

    def remove_event_handler(
        self,
        handler: Callable[[CallEvent], Awaitable[None]],
    ) -> None:
        """
        Remove an event handler.

        Args:
            handler: Handler to remove
        """
        if handler in self._event_handlers:
            self._event_handlers.remove(handler)

    async def _emit_event(self, event: CallEvent) -> None:
        """
        Emit an event to all registered handlers.

        Args:
            event: Event to emit
        """
        logger.debug(
            "voice_event",
            event_type=event.event_type.value,
            call_id=event.call_id,
        )

        for handler in self._event_handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.exception(
                    "voice_event_handler_error",
                    error=str(e),
                    event_type=event.event_type.value,
                )

    # ==================== Call Management ====================

    def get_active_calls(self) -> List[VoiceCall]:
        """Get all active calls."""
        return list(self._active_calls.values())

    def get_call(self, call_id: str) -> Optional[VoiceCall]:
        """Get a specific call by ID."""
        return self._active_calls.get(call_id)

    def _register_call(self, call: VoiceCall) -> None:
        """Register a call as active."""
        self._active_calls[call.call_id] = call

    def _unregister_call(self, call_id: str) -> None:
        """Remove a call from active calls."""
        self._active_calls.pop(call_id, None)

    @property
    def is_initialized(self) -> bool:
        """Check if provider is initialized."""
        return self._initialized


class MockVoiceProvider(VoiceProvider):
    """
    Mock voice provider for testing.

    Does not make actual calls - logs actions and simulates responses.
    Useful for development and testing workflows without real provider.
    """

    def __init__(self, config: VoiceConfig):
        super().__init__(config)
        self._call_counter = 0

    async def initialize(self) -> None:
        """Initialize mock provider."""
        logger.info("mock_voice_provider_initialized")
        self._initialized = True

    async def shutdown(self) -> None:
        """Shutdown mock provider."""
        # End all active calls
        for call_id in list(self._active_calls.keys()):
            await self.end_call(call_id)
        logger.info("mock_voice_provider_shutdown")
        self._initialized = False

    async def make_call(
        self,
        to_number: str,
        from_number: Optional[str] = None,
        **kwargs: Any,
    ) -> VoiceCall:
        """Simulate making a call."""
        self._call_counter += 1
        call_id = f"mock_call_{self._call_counter}"

        call = VoiceCall(
            call_id=call_id,
            state=CallState.INITIATING,
            from_number=from_number or "+10000000000",
            to_number=to_number,
            direction="outbound",
            started_at=datetime.utcnow(),
            metadata=kwargs,
        )

        self._register_call(call)

        await self._emit_event(CallEvent(
            event_type=CallEventType.CALL_INITIATED,
            call_id=call_id,
            data={"to": to_number, "from": from_number},
        ))

        logger.info(
            "mock_call_initiated",
            call_id=call_id,
            to=to_number,
            from_number=from_number,
        )

        # Simulate call connecting after short delay
        asyncio.create_task(self._simulate_call_progress(call_id))

        return call

    async def _simulate_call_progress(self, call_id: str) -> None:
        """Simulate call state transitions."""
        await asyncio.sleep(1)

        call = self._active_calls.get(call_id)
        if not call:
            return

        # Ringing
        call.state = CallState.RINGING
        await self._emit_event(CallEvent(
            event_type=CallEventType.CALL_RINGING,
            call_id=call_id,
        ))

        await asyncio.sleep(2)

        # Connected
        call = self._active_calls.get(call_id)
        if not call:
            return

        call.state = CallState.CONNECTED
        call.answered_at = datetime.utcnow()
        await self._emit_event(CallEvent(
            event_type=CallEventType.CALL_ANSWERED,
            call_id=call_id,
        ))

    async def answer_call(self, call_id: str, **kwargs: Any) -> bool:
        """Simulate answering a call."""
        call = self._active_calls.get(call_id)
        if not call:
            return False

        call.state = CallState.CONNECTED
        call.answered_at = datetime.utcnow()

        await self._emit_event(CallEvent(
            event_type=CallEventType.CALL_ANSWERED,
            call_id=call_id,
        ))

        logger.info("mock_call_answered", call_id=call_id)
        return True

    async def end_call(self, call_id: str, **kwargs: Any) -> bool:
        """Simulate ending a call."""
        call = self._active_calls.get(call_id)
        if not call:
            return False

        call.state = CallState.ENDED
        call.ended_at = datetime.utcnow()

        if call.answered_at:
            call.duration_seconds = int(
                (call.ended_at - call.answered_at).total_seconds()
            )

        await self._emit_event(CallEvent(
            event_type=CallEventType.CALL_ENDED,
            call_id=call_id,
            data={"duration": call.duration_seconds},
        ))

        self._unregister_call(call_id)
        logger.info("mock_call_ended", call_id=call_id)
        return True

    async def hold_call(self, call_id: str, hold: bool = True, **kwargs: Any) -> bool:
        """Simulate holding/resuming a call."""
        call = self._active_calls.get(call_id)
        if not call:
            return False

        if hold:
            call.state = CallState.ON_HOLD
            await self._emit_event(CallEvent(
                event_type=CallEventType.HOLD_STARTED,
                call_id=call_id,
            ))
        else:
            call.state = CallState.CONNECTED
            await self._emit_event(CallEvent(
                event_type=CallEventType.HOLD_ENDED,
                call_id=call_id,
            ))

        logger.info("mock_call_hold", call_id=call_id, hold=hold)
        return True

    async def transfer_call(
        self,
        call_id: str,
        to_number: str,
        **kwargs: Any,
    ) -> bool:
        """Simulate transferring a call."""
        call = self._active_calls.get(call_id)
        if not call:
            return False

        call.state = CallState.TRANSFERRING
        await self._emit_event(CallEvent(
            event_type=CallEventType.TRANSFER_INITIATED,
            call_id=call_id,
            data={"transfer_to": to_number},
        ))

        # Simulate transfer completion
        await asyncio.sleep(1)

        await self._emit_event(CallEvent(
            event_type=CallEventType.TRANSFER_COMPLETED,
            call_id=call_id,
            data={"transfer_to": to_number},
        ))

        logger.info("mock_call_transferred", call_id=call_id, to=to_number)
        return True

    async def play_audio(
        self,
        call_id: str,
        audio_url: Optional[str] = None,
        text: Optional[str] = None,
        **kwargs: Any,
    ) -> bool:
        """Simulate playing audio/TTS."""
        call = self._active_calls.get(call_id)
        if not call:
            return False

        logger.info(
            "mock_play_audio",
            call_id=call_id,
            audio_url=audio_url,
            text=text[:50] if text else None,
        )
        return True

    async def start_recording(self, call_id: str, **kwargs: Any) -> bool:
        """Simulate starting recording."""
        call = self._active_calls.get(call_id)
        if not call:
            return False

        await self._emit_event(CallEvent(
            event_type=CallEventType.RECORDING_STARTED,
            call_id=call_id,
        ))

        logger.info("mock_recording_started", call_id=call_id)
        return True

    async def stop_recording(self, call_id: str, **kwargs: Any) -> Optional[str]:
        """Simulate stopping recording."""
        call = self._active_calls.get(call_id)
        if not call:
            return None

        recording_url = f"https://mock-recordings.example.com/{call_id}.wav"
        call.recording_url = recording_url

        await self._emit_event(CallEvent(
            event_type=CallEventType.RECORDING_STOPPED,
            call_id=call_id,
            data={"recording_url": recording_url},
        ))

        logger.info("mock_recording_stopped", call_id=call_id)
        return recording_url

    async def send_dtmf(self, call_id: str, digits: str, **kwargs: Any) -> bool:
        """Simulate sending DTMF."""
        call = self._active_calls.get(call_id)
        if not call:
            return False

        logger.info("mock_dtmf_sent", call_id=call_id, digits=digits)
        return True

    async def get_call_status(self, call_id: str) -> Optional[VoiceCall]:
        """Get call status."""
        return self._active_calls.get(call_id)
