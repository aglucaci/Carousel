"""Digests of a radar's runs: Markdown, RSS, and email (plan task 20). See :mod:`carousel.notifications.digest`."""

from carousel.notifications.digest import (
    Digest,
    DigestItem,
    arrivals_from_store,
    digest_from_run,
    to_email,
    to_markdown,
    to_rss,
)
from carousel.notifications.mail import DeliveryError, SmtpSettings, send_email

__all__ = [
    "DeliveryError",
    "Digest",
    "DigestItem",
    "SmtpSettings",
    "arrivals_from_store",
    "digest_from_run",
    "send_email",
    "to_email",
    "to_markdown",
    "to_rss",
]
