"""Resend digest delivery with idempotency."""
from __future__ import annotations

import logging
import os
from datetime import date

import resend
from resend.exceptions import ResendError

from .models import RenderedDigest

logger = logging.getLogger(__name__)


class DigestSendError(RuntimeError):
    pass


def send_digest(rendered: RenderedDigest, *, run_date: date) -> str:
    """Send the digest via Resend. Returns the Resend message ID on success."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        raise DigestSendError("RESEND_API_KEY not set")
    resend.api_key = api_key

    from_email = os.environ.get("DIGEST_FROM_EMAIL")
    to_email = os.environ.get("DIGEST_TO_EMAIL")
    if not from_email or not to_email:
        raise DigestSendError("DIGEST_FROM_EMAIL and DIGEST_TO_EMAIL must be set")

    params: resend.Emails.SendParams = {
        "from": from_email,
        "to": [to_email],
        "reply_to": to_email,
        "subject": rendered.subject,
        "html": rendered.html,
        "text": rendered.text,
        "tags": [{"name": "kind", "value": "digest"}],
    }
    options: resend.Emails.SendOptions = {
        "idempotency_key": f"digest-{run_date.isoformat()}",
    }
    try:
        response = resend.Emails.send(params, options)
    except ResendError as e:
        logger.error(
            "resend send failed",
            extra={
                "code": getattr(e, "code", None),
                "error_type": getattr(e, "error_type", None),
                "message": getattr(e, "message", str(e)),
            },
        )
        raise DigestSendError(str(e)) from e
    message_id = response.get("id") if isinstance(response, dict) else getattr(response, "id", None)
    logger.info("digest sent", extra={"message_id": message_id, "subject": rendered.subject})
    return message_id or ""
