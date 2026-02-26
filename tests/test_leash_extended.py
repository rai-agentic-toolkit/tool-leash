"""
Extended coverage for tool-leash: async coroutines, sync generators, token budgets,
custom validators, serialization utilities, and exception attributes.
"""

import pytest

from tool_leash import (
    Budget,
    CallBlockedError,
    CallGuard,
    LeashBudgetExceeded,
    LeashError,
    leash,
)
from tool_leash.serialization import deep_search_dict, deep_serialize, estimate_tokens_safely

# ---------------------------------------------------------------------------
# Async coroutine path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_coroutine_tracks_calls():
    budget = Budget(max_calls=2)

    @leash(budget=budget)
    async def fetch(url: str) -> str:
        return f"data:{url}"

    assert await fetch("http://a.com") == "data:http://a.com"
    assert budget.calls_used == 1
    await fetch("http://b.com")
    assert budget.calls_used == 2


@pytest.mark.asyncio
async def test_async_coroutine_budget_exhaustion():
    budget = Budget(max_calls=1)

    @leash(budget=budget)
    async def op() -> int:
        return 42

    assert await op() == 42
    with pytest.raises(LeashBudgetExceeded):
        await op()


@pytest.mark.asyncio
async def test_async_coroutine_hitl_blocking():
    policy = CallGuard(restricted_args={"cmd": ["rm -rf"]})

    @leash(hitl=policy)
    async def run_cmd(cmd: str) -> str:
        return cmd

    assert await run_cmd("ls -la") == "ls -la"
    with pytest.raises(CallBlockedError):
        await run_cmd("rm -rf /")


@pytest.mark.asyncio
async def test_async_coroutine_return_value_preserved():
    """Return value flows through the decorator untouched."""
    budget = Budget(max_calls=5)

    @leash(budget=budget)
    async def compute(x: int, y: int) -> dict:
        return {"sum": x + y, "product": x * y}

    result = await compute(3, 4)
    assert result == {"sum": 7, "product": 12}


# ---------------------------------------------------------------------------
# Sync generator path
# ---------------------------------------------------------------------------


def test_sync_generator_yields_all_items():
    budget = Budget(max_calls=1)

    @leash(budget=budget)
    def stream():
        yield "a"
        yield "b"
        yield "c"

    assert list(stream()) == ["a", "b", "c"]
    assert budget.calls_used == 1


def test_sync_generator_budget_exhaustion_before_first_yield():
    budget = Budget(max_calls=0)

    @leash(budget=budget)
    def stream():
        yield "never"  # pragma: no cover

    with pytest.raises(LeashBudgetExceeded):
        list(stream())


def test_sync_generator_is_lazy():
    """The generator must remain lazy — items yielded one at a time."""
    budget = Budget(max_calls=1)
    log = []

    @leash(budget=budget)
    def counter():
        for i in range(3):
            log.append(i)
            yield i

    gen = counter()
    assert log == []  # nothing consumed yet
    assert next(gen) == 0
    assert log == [0]
    assert next(gen) == 1


def test_sync_generator_hitl_blocks_mid_stream():
    policy = CallGuard(restricted_args={"item": ["DANGER"]})

    @leash(hitl=policy)
    def safe_stream(item: str):
        yield item

    with pytest.raises(CallBlockedError):
        list(safe_stream("DANGER payload"))


# ---------------------------------------------------------------------------
# Token-only budget (no call limit)
# ---------------------------------------------------------------------------


def test_token_budget_consumed_on_output():
    budget = Budget(max_tokens=10_000)

    @leash(budget=budget)
    def echo(text: str) -> str:
        return text

    echo("hello world")
    assert budget.tokens_used > 0
    assert budget.calls_used == 0  # no call limit tracked


def test_token_budget_exhaustion_on_large_output():
    budget = Budget(max_tokens=5)

    @leash(budget=budget)
    def big_return() -> str:
        return "x" * 500

    with pytest.raises(LeashBudgetExceeded):
        big_return()


def test_token_budget_independent_of_call_limit():
    """Separate max_calls and max_tokens limits operate independently."""
    budget = Budget(max_calls=10, max_tokens=10_000)

    @leash(budget=budget)
    def noop() -> str:
        return "ok"

    for _ in range(5):
        noop()

    assert budget.calls_used == 5
    assert budget.tokens_used > 0
    assert budget.get_remaining_calls() == 5


# ---------------------------------------------------------------------------
# Custom tokenizer function
# ---------------------------------------------------------------------------


def test_custom_tokenizer_is_used():
    called_with = []

    def my_tokenizer(text: str) -> int:
        called_with.append(text)
        return len(text)  # 1 token per character

    budget = Budget(max_tokens=10_000)

    @leash(budget=budget, tokenizer_func=my_tokenizer)
    def work(x: str) -> str:
        return x

    work("hello")
    assert any("hello" in s for s in called_with)


def test_custom_tokenizer_budget_enforced():
    """With 1 token per char and max_tokens=3, a 20-char result should fail."""

    def char_tokenizer(text: str) -> int:
        return len(text)

    budget = Budget(max_tokens=3)

    @leash(budget=budget, tokenizer_func=char_tokenizer)
    def big() -> str:
        return "a" * 20

    with pytest.raises(LeashBudgetExceeded):
        big()


# ---------------------------------------------------------------------------
# HITL custom validator
# ---------------------------------------------------------------------------


def test_hitl_custom_validator_blocks():
    def no_large_payloads(args: dict) -> None:
        import json

        if len(json.dumps(args)) > 100:
            raise CallBlockedError(
                message="Payload too large",
                tool_name="unknown",
                trigger_reason="Payload exceeds 100 chars",
            )

    policy = CallGuard(custom_validator=no_large_payloads)

    @leash(hitl=policy)
    def ingest(data: str) -> str:
        return data

    # Small payload passes
    assert ingest("hi") == "hi"

    # Oversized payload blocked
    with pytest.raises(CallBlockedError):
        ingest("x" * 200)


def test_hitl_custom_validator_runs_before_restricted_args():
    """Custom validator is called first (top priority)."""
    calls = []

    def recording_validator(args: dict) -> None:
        calls.append("custom")

    policy = CallGuard(
        restricted_args={"q": ["DROP"]},
        custom_validator=recording_validator,
    )

    @leash(hitl=policy)
    def safe(q: str) -> str:
        return q

    safe("SELECT 1")
    assert "custom" in calls


def test_hitl_no_policy_is_passthrough():
    """No budget, no HITL — decorator is transparent."""

    @leash()
    def identity(x):
        return x

    assert identity(42) == 42
    assert identity("hello") == "hello"


# ---------------------------------------------------------------------------
# Budget remaining / state
# ---------------------------------------------------------------------------


def test_budget_remaining_calls_decrements():
    budget = Budget(max_calls=5)

    @leash(budget=budget)
    def op():
        return None

    assert budget.get_remaining_calls() == 5
    op()
    assert budget.get_remaining_calls() == 4
    op()
    op()
    assert budget.get_remaining_calls() == 2


def test_budget_remaining_returns_none_when_unlimited():
    budget = Budget()  # no limits
    assert budget.get_remaining_calls() is None
    assert budget.get_remaining_tokens() is None


def test_budget_remaining_tokens_decrements():
    budget = Budget(max_tokens=10_000)

    @leash(budget=budget)
    def echo(x: str) -> str:
        return x

    echo("hello")
    used = budget.tokens_used
    assert used > 0
    assert budget.get_remaining_tokens() == 10_000 - used


def test_budget_unlimited_never_raises():
    budget = Budget()  # no max_calls, no max_tokens

    @leash(budget=budget)
    def always_ok() -> str:
        return "fine"

    for _ in range(1000):
        always_ok()  # should never raise


# ---------------------------------------------------------------------------
# Decorator metadata preservation
# ---------------------------------------------------------------------------


def test_leash_preserves_function_name():
    @leash()
    def my_named_function():
        """My docstring."""
        return 1

    assert my_named_function.__name__ == "my_named_function"
    assert my_named_function.__doc__ == "My docstring."


@pytest.mark.asyncio
async def test_leash_preserves_async_function_name():
    @leash()
    async def my_async_func():
        """Async doc."""
        return 2

    assert my_async_func.__name__ == "my_async_func"
    assert my_async_func.__doc__ == "Async doc."


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


def test_leash_budget_exceeded_is_leash_error():
    assert issubclass(LeashBudgetExceeded, LeashError)


def test_hitl_yield_exception_is_leash_error():
    assert issubclass(CallBlockedError, LeashError)


def test_hitl_exception_attributes():
    exc = CallBlockedError(
        message="blocked",
        tool_name="my_tool",
        trigger_reason="matched DROP",
    )
    assert exc.tool_name == "my_tool"
    assert exc.trigger_reason == "matched DROP"
    assert str(exc) == "blocked"


def test_budget_exceeded_message_contains_limit():
    budget = Budget(max_calls=1)

    @leash(budget=budget)
    def op():
        return None

    op()
    with pytest.raises(LeashBudgetExceeded, match="max_calls"):
        op()


# ---------------------------------------------------------------------------
# deep_serialize edge cases
# ---------------------------------------------------------------------------


def test_deep_serialize_primitives():
    assert deep_serialize(None) is None
    assert deep_serialize(True) is True
    assert deep_serialize(42) == 42
    assert deep_serialize(3.14) == 3.14
    assert deep_serialize("hello") == "hello"


def test_deep_serialize_tuple_becomes_list():
    result = deep_serialize((1, 2, 3))
    assert result == [1, 2, 3]
    assert isinstance(result, list)


def test_deep_serialize_set_becomes_list():
    result = deep_serialize({1, 2})
    assert isinstance(result, list)
    assert set(result) == {1, 2}


def test_deep_serialize_nested_dict():
    result = deep_serialize({"a": {"b": {"c": 99}}})
    assert result == {"a": {"b": {"c": 99}}}


def test_deep_serialize_depth_limit():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": {"j": {"k": "leaf"}}}}}}}}}}}
    result = deep_serialize(deep, max_depth=3)
    result_str = str(result)
    assert "MaxDepthReached" in result_str


def test_deep_serialize_cycle_detection():
    d: dict = {}
    d["self"] = d
    result = deep_serialize(d)
    assert "CycleDetected" in str(result)


def test_deep_serialize_slots_object():
    class Slotted:
        __slots__ = ["value"]

        def __init__(self, v):
            self.value = v

    result = deep_serialize(Slotted("secret"))
    assert result == {"value": "secret"}


def test_deep_serialize_unknown_type_becomes_string():
    import datetime

    result = deep_serialize(datetime.date(2024, 1, 1))
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# deep_search_dict edge cases
# ---------------------------------------------------------------------------


def test_deep_search_dict_not_found():
    assert deep_search_dict({"a": 1}, "missing") == []


def test_deep_search_dict_top_level():
    result = deep_search_dict({"query": "SELECT 1"}, "query")
    assert result == ["SELECT 1"]


def test_deep_search_dict_inside_list():
    data = {"items": [{"query": "DROP TABLE x"}, {"query": "SELECT 1"}]}
    result = deep_search_dict(data, "query")
    assert "DROP TABLE x" in result
    assert "SELECT 1" in result


def test_deep_search_dict_depth_limit_returns_empty():
    data = {"l1": {"l2": {"l3": {"l4": {"target": "found"}}}}}
    # depth=2 should not reach l4
    result = deep_search_dict(data, "target", max_depth=2)
    assert result == []


def test_deep_search_dict_multiple_matches():
    data = {"q": "first", "nested": {"q": "second"}}
    result = deep_search_dict(data, "q")
    assert "first" in result
    assert "second" in result


# ---------------------------------------------------------------------------
# estimate_tokens_safely edge cases
# ---------------------------------------------------------------------------


def test_estimate_tokens_empty_string_is_at_least_one():
    assert estimate_tokens_safely("") >= 1


def test_estimate_tokens_string():
    # "hello" = 5 chars + 2 quotes = 7 chars → 7//4 = 1 (min 1)
    result = estimate_tokens_safely("hello")
    assert result >= 1


def test_estimate_tokens_none():
    assert estimate_tokens_safely(None) >= 1


def test_estimate_tokens_bool():
    assert estimate_tokens_safely(True) >= 1
    assert estimate_tokens_safely(False) >= 1


def test_estimate_tokens_empty_list():
    assert estimate_tokens_safely([]) >= 1


def test_estimate_tokens_empty_dict():
    assert estimate_tokens_safely({}) >= 1


def test_estimate_tokens_larger_payload_is_larger():
    small = estimate_tokens_safely("hi")
    large = estimate_tokens_safely("x" * 10_000)
    assert large > small


def test_estimate_tokens_dict_with_data():
    data = {"key": "value", "number": 42}
    result = estimate_tokens_safely(data)
    assert result >= 1
