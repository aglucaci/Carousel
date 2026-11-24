"""Sending digest emails over SMTP (stdlib ``smtplib``).

Settings come from the environment, never from a radar's config:

``CAROUSEL_SMTP_HOST``      required to send; without it every email delivery fails with a clear reason
``CAROUSEL_SMTP_PORT``      default 587 (465 with ``CAROUSEL_SMTP_SECURITY=ssl``)
``CAROUSEL_SMTP_SECURITY``  ``starttls`` (default), ``ssl``, or ``none`` (a local relay or test server)
``CAROUSEL_SMTP_USER``, ``CAROUSEL_SMTP_PASSWORD``  optional login
``CAROUSEL_SMTP_FROM``      sender address (default ``carousel@localhost``)

The password is never logged or recorded; delivery records keep only the
recipient, the outcome, and the server's error.
"""

from __future__ import annotations

import dataclasses
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable, Mapping, Optional


class DeliveryError(RuntimeError):
    """An email could not be delivered; the message says why (no secrets)."""


@dataclasses.dataclass(frozen=True)
class SmtpSettings:
    host: Optional[str]
    port: int
    security: str
    user: Optional[str]
    password: Optional[str] = dataclasses.field(repr=False)
    sender: str

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "SmtpSettings":
        env = os.environ if env is None else env
        security = (env.get("CAROUSEL_SMTP_SECURITY") or "starttls").lower()
        if security not in ("starttls", "ssl", "none"):
            raise DeliveryError(f"CAROUSEL_SMTP_SECURITY must be starttls, ssl, or none (got {security!r})")
        return cls(host=env.get("CAROUSEL_SMTP_HOST") or None,
                   port=int(env.get("CAROUSEL_SMTP_PORT") or (465 if security == "ssl" else 587)),
                   security=security, user=env.get("CAROUSEL_SMTP_USER") or None,
                   password=env.get("CAROUSEL_SMTP_PASSWORD") or None,
                   sender=env.get("CAROUSEL_SMTP_FROM") or "carousel@localhost")

    @property
    def configured(self) -> bool:
        return bool(self.host)


Transport = Callable[[SmtpSettings, EmailMessage], None]


def smtp_transport(settings: SmtpSettings, msg: EmailMessage) -> None:
    cls = smtplib.SMTP_SSL if settings.security == "ssl" else smtplib.SMTP
    kw = {"context": ssl.create_default_context()} if settings.security == "ssl" else {}
    with cls(settings.host, settings.port, timeout=30, **kw) as smtp:  # type: ignore[arg-type]
        if settings.security == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        if settings.user:
            smtp.login(settings.user, settings.password or "")
        smtp.send_message(msg)


def send_email(msg: EmailMessage, settings: Optional[SmtpSettings] = None, transport: Optional[Transport] = None) -> None:
    """Send one message, raising :class:`DeliveryError` with a readable reason on any failure."""
    settings = settings or SmtpSettings.from_env()
    if not settings.configured:
        raise DeliveryError("no SMTP server configured (set CAROUSEL_SMTP_HOST)")
    try:
        (transport or smtp_transport)(settings, msg)
    except smtplib.SMTPRecipientsRefused as exc:
        raise DeliveryError("recipient refused: " + "; ".join(f"{r}: {c} {m.decode(errors='replace') if isinstance(m, bytes) else m}"
                                                               for r, (c, m) in exc.recipients.items())) from None
    except smtplib.SMTPAuthenticationError as exc:
        raise DeliveryError(f"SMTP login failed ({exc.smtp_code})") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise DeliveryError(f"{type(exc).__name__}: {exc}") from None


__all__ = ["DeliveryError", "SmtpSettings", "send_email", "smtp_transport"]
