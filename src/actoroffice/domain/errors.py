class OfficeError(Exception):
    """Base error. `code` maps to an HTTP-ish status for channels/APIs."""

    code = 400


class NotFound(OfficeError):
    code = 404


class Conflict(OfficeError):
    """Someone else owns it, or it changed underneath you. Do not retry blindly."""

    code = 409


class InvalidTransition(OfficeError):
    code = 422


class PolicyDenied(OfficeError):
    code = 403
