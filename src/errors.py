"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    code = "domain_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(DomainError):
    code = "not_found"


class Conflict(DomainError):
    """聚合当前版本与调用方预期不一致。"""

    code = "version_conflict"

    def __init__(self, aggregate_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"聚合 {aggregate_id} 版本冲突：预期 {expected}，实际 {actual}",
            details={"aggregate_id": aggregate_id, "expected": expected, "actual": actual},
        )
        self.aggregate_id = aggregate_id
        self.expected = expected
        self.actual = actual


class DuplicateEvent(DomainError):
    code = "duplicate_event"


class ValidationFailure(DomainError):
    code = "validation_failure"

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message, details={"errors": errors or [message]})
        self.errors = errors or [message]


class GateRejected(DomainError):
    """发布门禁未通过：编辑不能越过专业复核对外定论。"""

    code = "gate_rejected"

    def __init__(self, errors: list[str], disputes: list[dict] | None = None) -> None:
        super().__init__("发布门禁未通过", details={"errors": errors, "disputes": disputes or []})
        self.errors = errors
        self.disputes = disputes or []
