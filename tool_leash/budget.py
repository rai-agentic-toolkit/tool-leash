import threading

from .exceptions import LeashBudgetExceeded


class Budget:
    """Manages the stateful execution budget for an agent."""

    def __init__(self, max_calls: int | None = None, max_tokens: int | None = None) -> None:
        self.max_calls: int | None = max_calls
        self.max_tokens: int | None = max_tokens
        self.calls_used: int = 0
        self.tokens_used: int = 0
        self._lock = threading.Lock()

    def consume_call(self) -> None:
        """Consume a single tool call from the budget."""
        if self.max_calls is not None:
            with self._lock:
                self.calls_used += 1
                if self.calls_used > self.max_calls:
                    raise LeashBudgetExceeded(
                        f"Budget exhausted: max_calls ({self.max_calls}) reached."
                    )

    def consume_tokens(self, tokens: int) -> None:
        """Consume a specific number of tokens from the budget."""
        if self.max_tokens is not None:
            with self._lock:
                self.tokens_used += tokens
                if self.tokens_used > self.max_tokens:
                    raise LeashBudgetExceeded(
                        f"Budget exhausted: consuming {tokens} tokens would exceed "
                        f"max_tokens ({self.max_tokens}). Tokens currently used: {self.tokens_used}."
                    )
                self.tokens_used += tokens

    def get_remaining_calls(self) -> int | None:
        if self.max_calls is None:
            return None
        with self._lock:
            return max(0, self.max_calls - self.calls_used)

    def get_remaining_tokens(self) -> int | None:
        if self.max_tokens is None:
            return None
        with self._lock:
            return max(0, self.max_tokens - self.tokens_used)
