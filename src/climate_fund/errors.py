"""领域错误：携带 HTTP 状态码，服务层与 API 层共用。"""


class DomainError(Exception):
    status = 400
    code = "domain_error"

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code


class ValidationError(DomainError):
    status = 400
    code = "validation_error"


class AuthError(DomainError):
    status = 401
    code = "unauthorized"


class PermissionError(DomainError):  # noqa: A001 - 领域内有意同名
    status = 403
    code = "forbidden"


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"
