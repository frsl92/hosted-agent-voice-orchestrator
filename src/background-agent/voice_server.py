"""ResponsesHostServer that also understands Voice Live requests (utterance stored in the conversation)."""

from __future__ import annotations

import logging

from agent_framework_foundry_hosting import ResponsesHostServer
from agent_framework_foundry_hosting._responses import _output_items_to_messages

logger = logging.getLogger(__name__)


class VoiceResponsesHostServer(ResponsesHostServer):
    async def _load_request_messages(self, context, *, approval_storage, configuration=None):
        messages = await super()._load_request_messages(
            context, approval_storage=approval_storage, configuration=configuration
        )
        if any(getattr(m, "role", None) == "user" and getattr(m, "text", "") for m in messages):
            return messages
        try:
            history = await context.get_history()
            history_messages = await _output_items_to_messages(history, approval_storage=approval_storage)
        except Exception:
            logger.exception("Could not read conversation history")
            return messages
        logger.info(
            "Empty input; conversation=%s history_items=%d", context.conversation_id, len(history_messages)
        )
        users = [m for m in history_messages if getattr(m, "role", None) == "user" and getattr(m, "text", "")]
        return [*messages, users[-1]] if users else messages
