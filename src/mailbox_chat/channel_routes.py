"""Mailbox-first channel-to-channel route controllers."""

from __future__ import annotations

from typing import Any

from .listener_registry import load_routes
from .runtime_admin import handle_admin_command


def dispatch_routes(mailbox: Any, *, listener_id: str, channel_id: str,
                    message: dict[str, Any]) -> None:
    if handle_admin_command(
        mailbox, listener_id=listener_id, channel_id=channel_id,
        author=str(message.get("author_id") or message.get("author") or ""),
        text=str(message.get("text") or ""),
    ):
        return
    context = {key: value for key, value in message.items() if key in {
        "origin_id", "origin_adapter", "origin_listener_id", "origin_source_id",
        "origin_channel_id", "origin_presence_id", "attachments", "author",
        "thread_id", "root_id", "source_id", "workflow_run_id", "correlation_id",
    }}
    for route in load_routes():
        source = route["source"]
        if not route["enabled"] or source.get("listener_id") != listener_id:
            continue
        if source.get("channel_id") and source.get("channel_id") != channel_id:
            continue
        controller = route["controller"]
        if controller["type"] == "relay_agent":
            mailbox.send(
                str(controller["mailbox_recipient"]), str(message.get("text") or ""),
                sender="channel-router", message_type="channel_route_request",
                extra_fields={**context, "route_id": route["id"],
                              "route_destinations": route["destinations"]},
            )
            continue
        for destination in route["destinations"]:
            mailbox.send(
                "channel-relay", str(message.get("text") or ""),
                sender=f"presence-controller:{route['id']}", message_type="channel_route_delivery",
                channel_type=str(destination.get("adapter") or ""),
                channel_id=str(destination.get("channel_id") or ""),
                extra_fields={
                    **context, "route_id": route["id"],
                    "listener_id": str(destination.get("listener_id") or ""),
                    "presence_id": str(destination.get("presence_id")
                                       or controller.get("presence_id") or ""),
                },
            )
