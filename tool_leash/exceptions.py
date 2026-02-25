class LeashError(Exception):
    """Base exception for all tool-leash errors."""

    pass


class LeashBudgetExceeded(LeashError):
    """Raised when a tool execution exceeds the allotted budget."""

    pass


class HITLYieldException(LeashError):
    """Raised when an operation requires Human-In-The-Loop approval."""

    def __init__(self, message: str, tool_name: str, trigger_reason: str):
        super().__init__(message)
        self.tool_name = tool_name
        self.trigger_reason = trigger_reason
