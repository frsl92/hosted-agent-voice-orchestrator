"""Let this agent serve calls that come from another Foundry agent.

The hosting layer insists on a platform-supplied end-user ID (``x-agent-user-id``). An agent calling this one with
its managed identity has no end user, so the platform sends none and every request is rejected. The Foundry session
is already private to the calling conversation, so when the user ID is missing we scope state by a fixed caller
label instead. Calls that do carry a user ID are untouched.
"""

from agent_framework_foundry_hosting import FoundryRequestScope

AGENT_CALLER_ID = "calling-agent"

_original = FoundryRequestScope.from_context.__func__


def _from_context(cls, config, context, **kwargs):
    if config.is_hosted and not context.user_id:
        context.user_id = AGENT_CALLER_ID
    return _original(cls, config, context, **kwargs)


FoundryRequestScope.from_context = classmethod(_from_context)
