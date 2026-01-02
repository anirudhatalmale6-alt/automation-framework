"""
Voice Handlers - Integration between voice provider and orchestrator.

Connects voice call events to the rules engine and workflow system.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional, Callable, Awaitable, Dict, List
from dataclasses import dataclass
import structlog

from voice.interface import (
    VoiceProvider,
    VoiceCall,
    VoiceConfig,
    CallState,
    CallEvent,
    CallEventType,
)

logger = structlog.get_logger()


@dataclass
class VoiceAction:
    """A voice action to execute."""
    action_type: str  # "make_call", "end_call", "play_audio", etc.
    params: Dict[str, Any]


class VoiceHandlers:
    """
    Voice call handlers for orchestrator integration.

    Connects voice provider events to the supervisor and
    provides action handlers for voice-related workflow steps.
    """

    def __init__(
        self,
        provider: VoiceProvider,
        supervisor: Any = None,  # Optional supervisor for event routing
    ):
        """
        Initialize voice handlers.

        Args:
            provider: Voice provider instance
            supervisor: Optional supervisor for event callbacks
        """
        self.provider = provider
        self.supervisor = supervisor

        # Register event handler
        self.provider.add_event_handler(self._handle_call_event)

        # Pending call workflows (call_id -> workflow context)
        self._call_workflows: Dict[str, Dict[str, Any]] = {}

    async def initialize(self) -> None:
        """Initialize the voice provider."""
        await self.provider.initialize()
        logger.info("voice_handlers_initialized")

    async def shutdown(self) -> None:
        """Shutdown voice handlers and provider."""
        await self.provider.shutdown()
        logger.info("voice_handlers_shutdown")

    # ==================== Action Handlers ====================
    # These can be registered with the orchestrator

    def get_action_handlers(self) -> Dict[str, Callable]:
        """
        Get action handlers for orchestrator registration.

        Returns:
            Dictionary of action_name -> handler function
        """
        return {
            "voice_make_call": self._action_make_call,
            "voice_end_call": self._action_end_call,
            "voice_play_audio": self._action_play_audio,
            "voice_play_tts": self._action_play_tts,
            "voice_hold": self._action_hold,
            "voice_transfer": self._action_transfer,
            "voice_send_dtmf": self._action_send_dtmf,
            "voice_start_recording": self._action_start_recording,
            "voice_stop_recording": self._action_stop_recording,
            "voice_get_status": self._action_get_status,
        }

    async def _action_make_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Make an outbound call.

        Params:
            to_number: Destination phone number (required)
            from_number: Caller ID (optional)
            workflow_context: Context to associate with call (optional)

        Returns:
            call_id, state, success
        """
        to_number = params.get("to_number")
        if not to_number:
            return {"success": False, "error": "to_number is required"}

        from_number = params.get("from_number")
        workflow_context = params.get("workflow_context", {})

        try:
            call = await self.provider.make_call(
                to_number=to_number,
                from_number=from_number,
            )

            # Store workflow context for this call
            if workflow_context:
                self._call_workflows[call.call_id] = workflow_context

            return {
                "success": True,
                "call_id": call.call_id,
                "state": call.state.value,
            }

        except Exception as e:
            logger.exception("voice_make_call_error", error=str(e))
            return {"success": False, "error": str(e)}

    async def _action_end_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        End a call.

        Params:
            call_id: Call to end (required)

        Returns:
            success, call_id
        """
        call_id = params.get("call_id")
        if not call_id:
            return {"success": False, "error": "call_id is required"}

        try:
            success = await self.provider.end_call(call_id)
            return {"success": success, "call_id": call_id}

        except Exception as e:
            logger.exception("voice_end_call_error", error=str(e))
            return {"success": False, "error": str(e)}

    async def _action_play_audio(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Play audio file on a call.

        Params:
            call_id: Call ID (required)
            audio_url: URL of audio file (required)

        Returns:
            success
        """
        call_id = params.get("call_id")
        audio_url = params.get("audio_url")

        if not call_id or not audio_url:
            return {"success": False, "error": "call_id and audio_url are required"}

        try:
            success = await self.provider.play_audio(
                call_id=call_id,
                audio_url=audio_url,
            )
            return {"success": success}

        except Exception as e:
            logger.exception("voice_play_audio_error", error=str(e))
            return {"success": False, "error": str(e)}

    async def _action_play_tts(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Play text-to-speech on a call.

        Params:
            call_id: Call ID (required)
            text: Text to speak (required)
            voice: Voice ID (optional)
            language: Language code (optional)

        Returns:
            success
        """
        call_id = params.get("call_id")
        text = params.get("text")

        if not call_id or not text:
            return {"success": False, "error": "call_id and text are required"}

        try:
            success = await self.provider.play_audio(
                call_id=call_id,
                text=text,
                voice=params.get("voice"),
                language=params.get("language"),
            )
            return {"success": success}

        except Exception as e:
            logger.exception("voice_play_tts_error", error=str(e))
            return {"success": False, "error": str(e)}

    async def _action_hold(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Put call on hold or resume.

        Params:
            call_id: Call ID (required)
            hold: True to hold, False to resume (default: True)

        Returns:
            success
        """
        call_id = params.get("call_id")
        if not call_id:
            return {"success": False, "error": "call_id is required"}

        hold = params.get("hold", True)

        try:
            success = await self.provider.hold_call(call_id, hold=hold)
            return {"success": success, "on_hold": hold}

        except Exception as e:
            logger.exception("voice_hold_error", error=str(e))
            return {"success": False, "error": str(e)}

    async def _action_transfer(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transfer call to another number.

        Params:
            call_id: Call ID (required)
            to_number: Transfer destination (required)

        Returns:
            success
        """
        call_id = params.get("call_id")
        to_number = params.get("to_number")

        if not call_id or not to_number:
            return {"success": False, "error": "call_id and to_number are required"}

        try:
            success = await self.provider.transfer_call(call_id, to_number)
            return {"success": success, "transferred_to": to_number}

        except Exception as e:
            logger.exception("voice_transfer_error", error=str(e))
            return {"success": False, "error": str(e)}

    async def _action_send_dtmf(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send DTMF tones.

        Params:
            call_id: Call ID (required)
            digits: DTMF digits (required)

        Returns:
            success
        """
        call_id = params.get("call_id")
        digits = params.get("digits")

        if not call_id or not digits:
            return {"success": False, "error": "call_id and digits are required"}

        try:
            success = await self.provider.send_dtmf(call_id, digits)
            return {"success": success, "digits_sent": digits}

        except Exception as e:
            logger.exception("voice_send_dtmf_error", error=str(e))
            return {"success": False, "error": str(e)}

    async def _action_start_recording(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Start call recording.

        Params:
            call_id: Call ID (required)

        Returns:
            success
        """
        call_id = params.get("call_id")
        if not call_id:
            return {"success": False, "error": "call_id is required"}

        try:
            success = await self.provider.start_recording(call_id)
            return {"success": success}

        except Exception as e:
            logger.exception("voice_start_recording_error", error=str(e))
            return {"success": False, "error": str(e)}

    async def _action_stop_recording(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Stop call recording and get URL.

        Params:
            call_id: Call ID (required)

        Returns:
            success, recording_url
        """
        call_id = params.get("call_id")
        if not call_id:
            return {"success": False, "error": "call_id is required"}

        try:
            recording_url = await self.provider.stop_recording(call_id)
            return {
                "success": recording_url is not None,
                "recording_url": recording_url,
            }

        except Exception as e:
            logger.exception("voice_stop_recording_error", error=str(e))
            return {"success": False, "error": str(e)}

    async def _action_get_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get call status.

        Params:
            call_id: Call ID (optional - returns all active if not specified)

        Returns:
            call info or list of active calls
        """
        call_id = params.get("call_id")

        if call_id:
            call = await self.provider.get_call_status(call_id)
            if call:
                return {"success": True, "call": call.to_dict()}
            return {"success": False, "error": "Call not found"}

        # Return all active calls
        active_calls = self.provider.get_active_calls()
        return {
            "success": True,
            "active_calls": [c.to_dict() for c in active_calls],
            "count": len(active_calls),
        }

    # ==================== Event Handling ====================

    async def _handle_call_event(self, event: CallEvent) -> None:
        """
        Handle events from voice provider.

        Routes events to supervisor for rule processing.
        """
        logger.info(
            "voice_event_received",
            event_type=event.event_type.value,
            call_id=event.call_id,
        )

        # Get workflow context for this call
        workflow_context = self._call_workflows.get(event.call_id, {})

        # Build trigger event for rules engine
        trigger_data = {
            "source": "voice",
            "event_type": event.event_type.value,
            "call_id": event.call_id,
            "timestamp": event.timestamp.isoformat(),
            "workflow_context": workflow_context,
            **event.data,
        }

        # Route to supervisor if available
        if self.supervisor:
            try:
                # The supervisor can process this as a trigger event
                # that may match rules and initiate workflows
                if hasattr(self.supervisor, "process_trigger"):
                    await self.supervisor.process_trigger(
                        trigger_type="voice_event",
                        data=trigger_data,
                    )
            except Exception as e:
                logger.exception("voice_event_routing_error", error=str(e))

        # Cleanup on call end
        if event.event_type in (CallEventType.CALL_ENDED, CallEventType.CALL_FAILED):
            self._call_workflows.pop(event.call_id, None)

    # ==================== Convenience Methods ====================

    async def make_call(
        self,
        to_number: str,
        from_number: Optional[str] = None,
        workflow_context: Optional[Dict[str, Any]] = None,
    ) -> VoiceCall:
        """
        Convenience method to make a call.

        Args:
            to_number: Destination number
            from_number: Caller ID
            workflow_context: Context to associate with call

        Returns:
            VoiceCall object
        """
        call = await self.provider.make_call(to_number, from_number)

        if workflow_context:
            self._call_workflows[call.call_id] = workflow_context

        return call

    def get_active_calls(self) -> List[VoiceCall]:
        """Get all active calls."""
        return self.provider.get_active_calls()
