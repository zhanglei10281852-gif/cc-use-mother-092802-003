"""领域错误类型。

每类错误携带对应的 HTTP 状态码，供 API 层统一映射。
"""


class GrantError(Exception):
    """拨款领域错误基类。"""

    http_status = 400


class ValidationError(GrantError):
    """入参不合法（金额非正、预算不平等等）。"""

    http_status = 400


class NotFoundError(GrantError):
    """实体不存在。"""

    http_status = 404


class PermissionDenied(GrantError):
    """角色无权执行，或违反经办/审核分离（四眼原则）。"""

    http_status = 403


class StateError(GrantError):
    """当前状态不允许该操作（如项目暂停期间付款）。"""

    http_status = 409


class ConflictError(GrantError):
    """并发或幂等冲突（如重复审批、回执金额不一致）。"""

    http_status = 409
