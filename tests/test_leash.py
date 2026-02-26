import threading
from typing import Any

import pytest

from tool_leash import Budget, CallBlockedError, CallGuard, LeashBudgetExceeded, leash
from tool_leash.serialization import deep_serialize


def test_budget_exhaustion():
    budget = Budget(max_calls=2)

    @leash(budget=budget)
    def my_tool():
        return "success"

    assert my_tool() == "success"
    assert my_tool() == "success"

    with pytest.raises(LeashBudgetExceeded):
        my_tool()


def test_hitl_targeted_args():
    policy = CallGuard(restricted_args={"query": ["DROP", "DELETE"]})

    @leash(hitl=policy)
    def db_query(user_name: str, query: str):
        return f"query executed for {user_name}"

    assert (
        db_query(user_name="John Dropdown", query="SELECT * FROM users")
        == "query executed for John Dropdown"
    )

    with pytest.raises(CallBlockedError):
        db_query(user_name="admin", query="DROP TABLE users")


def test_hitl_deep_kwargs():
    policy = CallGuard(restricted_args={"query": ["DROP", "DELETE"]})

    @leash(hitl=policy)
    def db_flex(**kwargs):
        return f"Executing {kwargs}"

    with pytest.raises(CallBlockedError):
        db_flex(target="production", query="DROP TABLE users")

    with pytest.raises(CallBlockedError):
        db_flex(payload={"query": "DELETE FROM users"})


def test_thread_safety():
    budget = Budget(max_calls=100)

    @leash(budget=budget)
    def threaded_tool():
        return True

    threads = []
    successes = 0
    failures = 0

    def worker():
        nonlocal successes, failures
        try:
            threaded_tool()
            with threading.Lock():
                successes += 1
        except LeashBudgetExceeded:
            with threading.Lock():
                failures += 1

    for _ in range(105):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    assert successes == 100
    assert failures == 5


# ==========================================
# V4 Specific Stability and Memory Tests
# ==========================================


def test_v4_infinite_recursion_dos_protection():
    """Verify V4 cycle detection prevents python memory crashes on circular objects."""
    budget = Budget(max_calls=1)

    @leash(budget=budget)
    def return_circular_reference():
        d = {}
        d["self"] = d  # Create infinite cycle
        return d

    try:
        # If this crashes the test suite with RecursionError, V4 protection failed.
        result = return_circular_reference()
    except RecursionError:
        pytest.fail("Cycle detection failed. Hit infinite recursion.")

    # Verify the serializer caught it and returned the fallback string.
    assert "<CycleDetected" in str(deep_serialize(result))


def test_v4_massive_payload_oom_protection():
    """Verify V4 doesn't crash allocating huge json.dumps strings for token metering."""
    budget = Budget(max_tokens=2_000_000)  # Give it plenty of room

    @leash(budget=budget)
    def explode_memory():
        # A 5,000,000 item list. json.dumps on this would be massively expensive.
        # estimate_tokens_safely should gracefully handle this via sys.getsizeof heuristics.
        return ["a"] * 5_000_000

    with pytest.raises(LeashBudgetExceeded):
        explode_memory()

    # We should have consumed roughly 5M tokens exact calculation (["a"] * 5,000,000)
    assert budget.tokens_used > 4_000_000


@pytest.mark.asyncio
async def test_v4_async_generator_exception_flushing():
    """Verify V4 properly catches exceptions thrown mid-stream and records yielded tokens."""
    budget = Budget(max_tokens=100)

    @leash(budget=budget)
    async def crash_stream():
        yield "chunk 1"
        yield "chunk 2"
        # Simulate network or DB crash halfway through streaming
        raise ConnectionError("DB Died")

    gen = crash_stream()

    with pytest.raises(ConnectionError):
        async for _ in gen:
            pass

    # CRITICAL: Even though it crashed, the tokens for "chunk 1" and "chunk 2"
    # must still be deducted from the budget.
    assert budget.tokens_used > 0
    assert budget.tokens_used < 100


# ==========================================
# V6 Red Team Exploit Protections
# ==========================================


def test_v6_slots_evasion():
    """Verify that objects using __slots__ cannot evade deep_serialize."""

    class SlottedExploit:
        __slots__ = ["query"]

        def __init__(self):
            self.query = "DROP TABLE users"

    policy = CallGuard(restricted_args={"query": ["DROP"]})

    @leash(hitl=policy)
    def db_runner(payload: Any):
        pass

    with pytest.raises(CallBlockedError):
        db_runner(SlottedExploit())


def test_v6_input_generator_bypass():
    """Verify that malicious strings flowing IN via a generator are caught."""
    policy = CallGuard(restricted_args={"query": ["DROP"]})
    budget = Budget(max_tokens=2000)

    @leash(hitl=policy, budget=budget)
    def consume_stream(stream):
        for _item in stream:
            pass

    def malicious_generator():
        yield {"harmless": "data"}
        yield {"query": "DROP TABLE users"}

    with pytest.raises(CallBlockedError):
        consume_stream(malicious_generator())
