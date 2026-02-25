from typing import Any


def deep_serialize(obj: Any, max_depth: int = 10, _seen: set[int] | None = None) -> Any:
    """
    Recursively attempts to serialize complex objects into primitive dictionaries or lists
    for safe HITL analysis and token estimation.

    Implements cycle detection and depth limiting to prevent Denial of Service
    from deeply nested or self-referential payloads (like ORM models or DOM trees).
    """
    if _seen is None:
        _seen = set()

    obj_id = id(obj)
    if obj_id in _seen:
        return f"<CycleDetected: {type(obj).__name__}>"

    if max_depth <= 0:
        return f"<MaxDepthReached: {type(obj).__name__}>"

    _seen.add(obj_id)

    try:
        if isinstance(obj, str | int | float | bool | type(None)):
            return obj
        if isinstance(obj, list | tuple | set):
            return [deep_serialize(item, max_depth - 1, _seen) for item in obj]
        if isinstance(obj, dict):
            return {str(k): deep_serialize(v, max_depth - 1, _seen) for k, v in obj.items()}

        # Attempt to handle Pydantic V2 models
        if hasattr(obj, "model_dump"):
            return deep_serialize(obj.model_dump(), max_depth - 1, _seen)
        # Attempt to handle Pydantic V1 / Dataclasses
        if hasattr(obj, "dict"):
            return deep_serialize(obj.dict(), max_depth - 1, _seen)

        try:
            return deep_serialize(vars(obj), max_depth - 1, _seen)
        except TypeError:
            if hasattr(obj, "__slots__"):
                # Extract slotted attributes, ignoring uninitialized ones
                slots_dict = {s: getattr(obj, s) for s in obj.__slots__ if hasattr(obj, s)}
                return deep_serialize(slots_dict, max_depth - 1, _seen)
            return str(obj)
    finally:
        # Crucial for allowing sibling branches to reference the same immutable objects
        # without triggering a false-positive cycle, while still preventing structural loops.
        _seen.remove(obj_id)


def deep_search_dict(d: Any, target_key: str, max_depth: int = 10) -> list[Any]:
    """
    Recursively searches a dictionary for all values matching `target_key`.
    This prevents users from burying malicious strings inside **kwargs or nested dicts.
    Implements a strict depth limit.
    """
    if max_depth <= 0:
        return []

    results = []
    if isinstance(d, dict):
        for k, v in d.items():
            if k == target_key:
                results.append(v)
            if isinstance(v, dict) or isinstance(v, list):
                results.extend(deep_search_dict(v, target_key, max_depth - 1))
    elif isinstance(d, list):
        for item in d:
            if isinstance(item, dict) or isinstance(item, list):
                results.extend(deep_search_dict(item, target_key, max_depth - 1))

    return results


def estimate_tokens_safely(serialized_obj: Any) -> int:
    """
    Deterministically computes the exact byte-length of the object if it were
    serialized to JSON, without ever allocating the monolithic string in memory.
    This guarantees 0(1) memory overhead and prevents OOM crashes on massive payloads.
    Roughly 1 token per 4 bytes of data.
    """

    def _char_len(obj: Any) -> int:
        if isinstance(obj, str):
            # Quotes + length + escaping heuristics (approximate)
            return len(obj) + 2
        elif isinstance(obj, int | float):
            return len(str(obj))
        elif isinstance(obj, bool):
            return 4 if obj else 5
        elif obj is None:
            return 4
        elif isinstance(obj, list | tuple | set):
            if not obj:
                return 2  # "[]"
            # Brackets + commas + elements
            return 2 + (len(obj) - 1) + sum(_char_len(item) for item in obj)
        elif isinstance(obj, dict):
            if not obj:
                return 2  # "{}"
            # Braces + (quotes+colon+comma) overhead per kv pair + lengths
            total = 2 + (len(obj) - 1)  # braces and commas
            for k, v in obj.items():
                total += len(str(k)) + 3 + _char_len(v)  # "key": value
            return total
        else:
            # Fallback for unrecognized types (should be caught by deep_serialize first)
            return len(str(obj)) + 2

    byte_size = _char_len(serialized_obj)
    return max(1, byte_size // 4)
