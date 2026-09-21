"""业务错误类型。"""


class OrderServiceError(Exception):
    """业务错误基类，code 供接口层映射状态码。"""

    code = "bad_request"
    http_status = 400


class NotFoundError(OrderServiceError):
    code = "not_found"
    http_status = 404


class ConflictError(OrderServiceError):
    code = "conflict"
    http_status = 409


class ValidationError(OrderServiceError):
    code = "validation_error"
    http_status = 400


class StageError(ConflictError):
    """当前处置阶段不允许该操作。"""

    code = "invalid_stage"
