"""ACA-Py Pickup Protocol Plugin."""


import logging
import re
import json
from base64 import urlsafe_b64decode
from os import getenv
from typing import cast

from aries_cloudagent.config.injection_context import InjectionContext
from aries_cloudagent.core.event_bus import Event, EventBus
from aries_cloudagent.core.profile import Profile
from aries_cloudagent.core.protocol_registry import ProtocolRegistry
from aries_cloudagent.transport.outbound.message import OutboundMessage
from aries_cloudagent.transport.wire_format import BaseWireFormat
from redis import asyncio as aioredis

from .protocol.delivery import Delivery, DeliveryRequest, MessagesReceived
from .protocol.live_mode import LiveDeliveryChange
from .protocol.status import Status, StatusRequest
from .undelivered_queue.base import UndeliveredInterface
from .undelivered_queue.in_memory_queue import InMemoryQueue
from .undelivered_queue.redis_persisted_queue import RedisPersistedQueue

UNDELIVERABLE_EVENT_TOPIC = re.compile("acapy::outbound-message::undeliverable")
LOGGER = logging.getLogger(__name__)
REDIS_ENDPOINT = getenv("REDIS_ENDPOINT", "redis://redis:6379")


async def setup(context: InjectionContext):
    LOGGER.debug("Hit Pickup Plugin setup")
    """Setup plugin."""

    protocol_registry = context.inject(ProtocolRegistry)
    assert protocol_registry
    protocol_registry.register_message_types(
        {
            Status.message_type: Status,
            StatusRequest.message_type: StatusRequest,
            Delivery.message_type: Delivery,
            DeliveryRequest.message_type: DeliveryRequest,
            MessagesReceived.message_type: MessagesReceived,
            LiveDeliveryChange.message_type: LiveDeliveryChange,
        }
    )

    event_bus = context.inject(EventBus)
    event_bus.subscribe(UNDELIVERABLE_EVENT_TOPIC, undeliverable)

    settings = context.settings.for_plugin("pickup")
    persistence = settings.get("persistence")
    redis_uri = settings.get("redis", {}).get("server")
    ttl = settings.get("redis", {}).get("ttl_hours")

    if persistence == "mem":
        queue = InMemoryQueue()
    elif persistence == "redis":
        queue = RedisPersistedQueue(redis=await aioredis.from_url(redis_uri), ttl_seconds=ttl)
    else:
        raise ValueError()

    context.injector.bind_instance(UndeliveredInterface, queue)


async def setup_redis(settings):
    redis = settings.get("redis")

    if not redis:
        raise ValueError(
            "If redis persistence is chosen, redis data must be specified."
        )

    ttl = redis.get("ttl_hours")
    redis_uri = redis.get("server")

    if not redis_uri:
        raise ValueError("redis_uri must be specified.")

    return RedisPersistedQueue(
        redis=await aioredis.from_url(redis_uri), ttl_seconds=60 * 60 * ttl
    )


async def undeliverable(profile: Profile, event: Event):
    LOGGER.debug(
        "Undeliverable Event Captured in Pickup Protocol: %s, %s",
        event.topic,
        event.payload,
    )
    outbound = cast(OutboundMessage, event.payload)
    # This scenario is rare; a message will almost always have an
    # encrypted payload. The only time it won't is if we're sending a
    # message from the mediator itself, rather than forwarding a message
    # from another agent.
    # TODO: update ACA-Py to set encoded payload prior to emitting undelivered event

    if not outbound.enc_payload:
        wire_format = profile.inject(BaseWireFormat)
        recipient_key = outbound.target_list[0].recipient_keys
        routing_keys = outbound.target_list[0].routing_keys or []
        sender_key = outbound.target_list[0].sender_key
        async with profile.session() as profile_session:
            outbound.enc_payload = await wire_format.encode_message(
                profile_session,
                outbound.payload,
                recipient_key,
                routing_keys,
                sender_key,
            )

    protected_headers = json.loads(
        urlsafe_b64decode(json.loads(outbound.enc_payload)["protected"])
    )

    queue = profile.inject(UndeliveredInterface)
    for recipient in protected_headers["recipients"]:
        await queue.add_message(
            recipient_key=recipient["header"]["kid"], msg=outbound.enc_payload
        )
