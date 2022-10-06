"""Delivery Request and wrapper message for Pickup Protocol."""

import logging
from typing import Optional, Sequence, Set, cast

from aries_cloudagent.messaging.request_context import RequestContext
from aries_cloudagent.messaging.responder import BaseResponder
from aries_cloudagent.transport.inbound.manager import InboundTransportManager
from aries_cloudagent.transport.inbound.session import InboundSession
from pydantic import Field, conint
from typing_extensions import Annotated

from ..acapy import AgentMessage, Attach
from ..acapy.error import HandlerException
from ..undelivered_queue.base import UndeliveredQueue
from .status import Status

LOGGER = logging.getLogger(__name__)
PROTOCOL = "https://didcomm.org/messagepickup/2.0"


class DeliveryRequest(AgentMessage):
    """DeliveryRequest message."""

    message_type = f"{PROTOCOL}/delivery-request"

    limit: conint(gt=0)
    recipient_key: Optional[str] = None

    @staticmethod
    def determine_session(manager: InboundTransportManager, key: str):
        """Determine the session associated with the given key."""
        for session in manager.sessions.values():
            session = cast(InboundSession, session)
            if key in session.reply_verkeys:
                return session
        return None

    async def handle(self, context: RequestContext, responder: BaseResponder):
        """Handle DeliveryRequest message."""
        if not self.transport or self.transport.return_route != "all":
            raise HandlerException(
                "DeliveryRequest must have transport decorator with return "
                "route set to all"
            )

        queue = context.inject(UndeliveredQueue)

        key = context.message_receipt.sender_verkey
        message_attachments = []

        if await queue.has_message_for_key(key):

            for msg in await queue.get_messages_for_key(key, self.limit):

                attached_msg = Attach.data_base64(
                    ident=UndeliveredQueue.ident_from_message(msg).decode(),
                    value=msg,
                )
                message_attachments.append(attached_msg)

            response = Delivery(message_attachments=message_attachments)
            response.assign_thread_from(self)
            await responder.send_reply(response)

        else:
            response = Status(recipient_key=self.recipient_key, message_count=0)
            response.assign_thread_from(self)
            await responder.send_reply(response)


class Delivery(AgentMessage):
    """Message wrapper for delivering messages to a recipient."""

    class Config:
        allow_population_by_field_name = True

    message_type = f"{PROTOCOL}/delivery"

    recipient_key: Optional[str] = None
    message_attachments: Annotated[
        Sequence[Attach], Field(description="Attached messages", alias="~attach")
    ]


class MessagesReceived(AgentMessage):
    """MessageReceived acknowledgement message."""

    message_type = f"{PROTOCOL}/messages-received"
    message_id_list: Set[str]

    async def handle(self, context: RequestContext, responder: BaseResponder):
        """Handle MessageReceived message."""
        if not self.transport or self.transport.return_route != "all":
            raise HandlerException(
                "MessageReceived must have transport decorator with return "
                "route set to all"
            )

        queue = context.inject(UndeliveredQueue)
        key = context.message_receipt.sender_verkey

        if await queue.has_message_for_key(key):
            await queue.remove_messages_for_key(
                key, [msg_ident.encode() for msg_ident in self.message_id_list]
            )

        response = Status(message_count=await queue.message_count_for_key(key))
        response.assign_thread_from(self)
        await responder.send_reply(response)
