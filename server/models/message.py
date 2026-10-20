"""Relay messaging API schemas (Pydantic v2).

Matches routes/relay_routes.post_message, which reads msg.content and
msg.message_type and passes them to services.relay_service.send_message.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints

# The longest message a clinician may post, in characters, on BOTH lanes (REST
# here, the socket in ws/realtime.new_message). Sized to take a pasted discharge
# summary, which tests/test_room_fit_adversarial.py posts at 40 kB, while
# ending "unbounded": the socket lane had no ceiling at all.
MAX_MESSAGE_CHARS = 50_000

MessageContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS),
]


class MessageCreate(BaseModel):
    """Body for POST /relay/channels/{channel_id}/messages."""

    # Stripped, then 1 to MAX_MESSAGE_CHARS. An empty or whitespace-only post
    # used to be stored and broadcast as a blank message.
    content: MessageContent
    # Clinicians post text, and only text. The schema allows alert, system,
    # ambient_action and surgical_alert too, but those are written by the
    # server itself; taking the type from the request let any clinician forge
    # a system line, a structured alert that reaches the alerts feed, or a
    # hidden ambient_action card.
    message_type: Literal["text"] = "text"
