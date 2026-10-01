"""Server-issued approval tokens for executions that the PolicyEngine gates.

A token is HMAC-SHA256("<artifact_id>:<version>") under a server secret, so it
approves exactly one artifact version and cannot be made up by a caller. Tokens
are issued by POST /artifacts/{id}/versions/{version}/approve.
"""

import functools
import hashlib
import hmac
import logging
import secrets

from ritesmith.config import Settings

log = logging.getLogger(__name__)


@functools.cache
def _ephemeral_secret() -> str:
    log.warning(
        "no RITESMITH_APPROVAL_SECRET / API token / Trama token configured: approval tokens "
        "use a per-process secret and stop working after a restart"
    )
    return secrets.token_hex(32)


def _secret(settings: Settings) -> bytes:
    secret = settings.approval_secret or settings.api_token or settings.trama_token
    return (secret or _ephemeral_secret()).encode()


def issue_approval_token(settings: Settings, artifact_id: str, version: int) -> str:
    message = f"{artifact_id}:{version}".encode()
    return hmac.new(_secret(settings), message, hashlib.sha256).hexdigest()


def verify_approval_token(
    settings: Settings, artifact_id: str, version: int, token: str | None
) -> bool:
    if not token:
        return False
    return hmac.compare_digest(issue_approval_token(settings, artifact_id, version), token)
