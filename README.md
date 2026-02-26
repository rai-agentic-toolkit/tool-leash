# tool-leash

**Stateful execution budgets and call-guard policies for AI agent tools.**

> **Project Status:** This is a personal project exploring strict agentic security patterns. The organization name was chosen by my AI agent—there is no VC funding or sales team here, just code.

**Pure Python 3.10+. Zero dependencies.**

> See [CONSTITUTION.md](CONSTITUTION.md) for architectural security constraints.

## Why use this?

When building AI agents, you give the model access to tools (functions). Without guardrails, an agent can:

- Call the same tool infinitely (token burn / runaway costs)
- Execute dangerous operations without human approval
- Process massive payloads that crash your service

`tool-leash` wraps your tool functions with a decorator that enforces:

1. **Execution Budgets** — Hard limits on call count and token consumption
2. **Call Guard Policies** — Block execution when arguments contain restricted patterns (raises exception for your agent framework to handle)

## Install

```bash
pip install tool-leash

# Or install from source
git clone https://github.com/rai-agentic-toolkit/tool-leash.git
cd tool-leash
pip install -e .
```

## Quick Start

```python
from tool_leash import leash, Budget, CallGuard, LeashBudgetExceeded, CallBlockedError

# 1. Define your budget constraints
budget = Budget(max_calls=10, max_tokens=50000)

# 2. Define call guard policy for dangerous operations
policy = CallGuard(restricted_args={"query": ["DROP", "DELETE", "TRUNCATE"]})

# 3. Apply the leash decorator
@leash(budget=budget, hitl=policy)
def execute_sql(query: str):
    return f"Executed: {query}"

# Safe queries work fine
execute_sql("SELECT * FROM users")  # OK

# Dangerous queries are blocked
try:
    execute_sql("DROP TABLE users")
except CallBlockedError as e:
    print(f"Blocked: {e.trigger_reason}")
    # -> Matched restricted substring 'DROP' in argument 'query'
    # Your agent framework can catch this and request human approval

# Budget exhaustion stops runaway agents
for i in range(15):
    try:
        execute_sql(f"SELECT {i}")
    except LeashBudgetExceeded:
        print(f"Budget exhausted after {budget.calls_used} calls")
        break
```

## Core API

### Budget

Tracks call count and token consumption with thread-safe counters.

```python
from tool_leash import Budget

budget = Budget(
    max_calls=100,      # Maximum tool invocations
    max_tokens=500000   # Maximum tokens (input + output)
)

# Check remaining budget
print(budget.get_remaining_calls())   # -> 100
print(budget.get_remaining_tokens())  # -> 500000
```

### CallGuard

Evaluates tool arguments against restricted patterns. Raises `CallBlockedError` when a match is found.

```python
from tool_leash import CallGuard

# Block specific patterns in specific arguments
policy = CallGuard(
    restricted_args={
        "query": ["DROP", "DELETE", "TRUNCATE"],
        "path": ["/etc/", "/root/", "~/.ssh/"],
        "command": ["rm -rf", "sudo", "chmod 777"],
    }
)
```

#### Custom Validators

For complex validation logic, pass a custom validator function:

```python
from tool_leash import CallGuard, CallBlockedError

def block_large_payloads(args: dict) -> None:
    """Reject payloads over 1MB."""
    import json
    if len(json.dumps(args)) > 1_000_000:
        raise CallBlockedError(
            message="Payload too large",
            tool_name="unknown",
            trigger_reason="Payload exceeds 1MB limit"
        )

policy = CallGuard(custom_validator=block_large_payloads)
```

### The `@leash` Decorator

Wraps sync functions, async functions, generators, and async generators.

```python
from tool_leash import leash, Budget, CallGuard

budget = Budget(max_calls=50)
policy = CallGuard(restricted_args={"url": ["localhost", "127.0.0.1"]})

# Sync function
@leash(budget=budget, hitl=policy)
def fetch_url(url: str):
    return requests.get(url).text

# Async function
@leash(budget=budget, hitl=policy)
async def fetch_url_async(url: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.text()

# Generator (streaming)
@leash(budget=budget)
def stream_data():
    for chunk in large_dataset:
        yield chunk  # Each yield consumes tokens from budget

# Async generator
@leash(budget=budget)
async def stream_llm_response():
    async for token in llm.stream("Hello"):
        yield token
```

### Custom Tokenizer

By default, `tool-leash` estimates tokens using byte-length heuristics (~4 bytes per token). For accurate counting, pass your tokenizer:

```python
import tiktoken

encoder = tiktoken.encoding_for_model("gpt-4")

@leash(budget=budget, tokenizer_func=lambda s: len(encoder.encode(s)))
def my_tool(data: str):
    return process(data)
```

## What It Protects Against

| Threat | Protection |
| ------ | ---------- |
| **Runaway agents** | `max_calls` hard limit stops infinite loops |
| **Token burn attacks** | `max_tokens` caps total I/O consumption |
| **Dangerous operations** | Call guard policy blocks restricted patterns |
| **Deep nesting attacks** | `deep_serialize` has depth limits (default: 10) |
| **Circular reference DoS** | Cycle detection prevents `RecursionError` crashes |
| **Memory exhaustion** | Token estimation uses O(1) memory (no `json.dumps`) |
| **__slots__ evasion** | Serializer extracts slotted attributes |
| **Generator bypass** | Input generators are wrapped and checked per-item |

## Exception Hierarchy

```python
from tool_leash import LeashError, LeashBudgetExceeded, CallBlockedError

try:
    dangerous_tool()
except LeashBudgetExceeded:
    # Budget exhausted - stop the agent
    pass
except CallBlockedError as e:
    # Restricted pattern detected - handle in your agent framework
    print(e.tool_name)       # The blocked function name
    print(e.trigger_reason)  # Why it was blocked
except LeashError:
    # Catch-all for tool-leash errors
    pass
```

## Thread Safety

Budget counters use `threading.Lock` for safe concurrent access:

```python
import threading
from tool_leash import leash, Budget

budget = Budget(max_calls=100)

@leash(budget=budget)
def worker_task():
    return "done"

# Safe to call from multiple threads
threads = [threading.Thread(target=worker_task) for _ in range(100)]
for t in threads:
    t.start()
for t in threads:
    t.join()

# Exactly 100 calls consumed, no race conditions
assert budget.calls_used == 100
```

## Partner Integration: `secure-ingest`

`tool-leash` controls execution budgets and argument patterns, but it does not validate the structural integrity of incoming payloads.

For defense-in-depth, combine with **[`secure-ingest`](https://github.com/rai-agentic-toolkit/secure-ingest)** to validate payload structure *before* the tool is ever invoked:

```python
from secure_ingest import parse, ContentType
from tool_leash import leash, Budget, CallGuard

budget = Budget(max_calls=50, max_tokens=100000)
policy = CallGuard(restricted_args={"query": ["DROP", "DELETE"]})

@leash(budget=budget, hitl=policy)
def process_payload(data: dict):
    return execute_query(data["query"])

# 1. Validate structure at the boundary (secure-ingest)
result = parse(
    untrusted_json,
    ContentType.JSON,
    max_depth=5,
    max_size_bytes=10240
)

# 2. Execute with budget/call-guard constraints (tool-leash)
process_payload(result.content)
```

## Security Model

`tool-leash` operates as a **pre-execution constraint layer**:

- **Stateless evaluation** — Call guard policies are pure functions with no side effects
- **Graceful degradation** — Serialization handles cycles and depth limits with fallback strings rather than crashing
- **Defense-in-depth** — Designed to complement, not replace, other security layers

**What it doesn't do:**

- Runtime behavior monitoring (use observability tools)
- Network-level filtering (use firewalls)
- Semantic intent classification (use `secure-ingest` with custom validators)

## License

MIT

## Authors

Jesse Castro & Raven
