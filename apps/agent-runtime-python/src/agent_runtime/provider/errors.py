"""Provider 层的类型化失败（ADR-0005 §2）。

失败一律用异常表达而非返回值：返回值可以被忽略，异常迫使调用方处理，
或者让失败向上冒泡成明确的终态——不会出现「忘记检查返回值所以当成功了」。

每个异常绑定一个 failure_class，取值来自 contracts/events/README.md 的封闭枚举，
使 M6 的失败归因统计有据可依。
"""

from __future__ import annotations


class ProviderError(Exception):
    """Provider 层失败的基类。"""

    failure_class: str = "provider_failure"
    retriable: bool = False

    def __init__(self, message: str = "", *, attempt: int = 1) -> None:
        super().__init__(message or self.__class__.__name__)
        self.attempt = attempt


class ProviderTimeout(ProviderError):
    retriable = True

    def __init__(self, message: str = "provider request timed out", *, attempt: int = 1) -> None:
        super().__init__(message, attempt=attempt)


class ProviderRateLimited(ProviderError):
    retriable = True

    def __init__(
        self,
        message: str = "provider rate limited the request",
        *,
        attempt: int = 1,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, attempt=attempt)
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    """5xx。服务端瞬时故障，可重试。"""

    retriable = True


class ProviderAuthError(ProviderError):
    """401 / 403。配置问题而非瞬时故障，重试只会浪费预算并可能触发风控。"""

    failure_class = "authorization_failed"
    retriable = False

    def __init__(self, message: str = "provider rejected credentials", *, attempt: int = 1) -> None:
        super().__init__(message, attempt=attempt)


class ProviderBadResponse(ProviderError):
    """包络结构不对、内容为空、被截断、或 4xx 请求错误。重试不会改变结果。"""

    retriable = False


class ProviderContentFiltered(ProviderError):
    """被 Provider 的内容审核拒答。归入安全规则命中而非普通失败。"""

    failure_class = "safety_rule_triggered"
    retriable = False

    def __init__(self, message: str = "provider filtered the response", *, attempt: int = 1) -> None:
        super().__init__(message, attempt=attempt)


class ProviderBudgetExceeded(ProviderError):
    """进程级防呆闸。与 Run 层的业务预算是两回事，两层都要有。"""

    failure_class = "cost_budget_exhausted"
    retriable = False


class StructuredOutputError(Exception):
    """结构化输出解析失败。

    不属于 Provider 层：Provider 只负责拿到文本，是否符合业务 schema 由这一层判定。
    分层的意义在于「网络失败」该重试而「模型胡说」不该重试。
    """

    failure_class: str = "provider_failure"
    retriable: bool = False


class MalformedJsonError(StructuredOutputError):
    """不是合法 JSON。不做任何修补（ADR-0005 §3）。"""


class SchemaViolationError(StructuredOutputError):
    """是合法 JSON 但不符合 schema。不填默认值——填了就成了假成功。"""
