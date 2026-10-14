"""Errors shared across layers."""


class EasytopError(Exception):
    pass


class EndpointNotAllowed(EasytopError):
    """Connector refused an endpoint outside the read-only whitelist."""


class SessionExpired(EasytopError):
    """The Webtop session is no longer valid; the parent must reconnect."""


class UpstreamError(EasytopError):
    """Webtop failed, timed out or answered unexpectedly."""


class FileUnavailable(EasytopError):
    """A message attachment cannot be handed over. `reason` is a content-free label: `not_found` (message or
    file gone), `too_big`, `type_refused` (extension not allowed, or the bytes are not what the extension says)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ParseError(EasytopError):
    """Webtop answered with a shape we do not recognize (strict parsing)."""


class ParserNotImplemented(ParseError):
    """Parser is a stub: real parent API shape not known yet. Needs spike verification."""


class KidNotFound(EasytopError):
    """Kid id/name is not in this parent's own list."""


class SessionFileError(EasytopError):
    """The saved session file is missing, unreadable or holds no Webtop login. Never carries its content."""


class SessionDecryptError(EasytopError):
    """A stored session cannot be decrypted (wrong key or damaged data). Never carries the data."""
