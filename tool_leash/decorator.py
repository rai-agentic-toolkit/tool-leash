import functools
import inspect
import json
import logging
from collections.abc import AsyncGenerator, Callable, Generator
from typing import Any, TypeVar, cast

from .budget import Budget
from .hitl import HITLPolicy
from .serialization import deep_serialize, estimate_tokens_safely

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def leash(
    budget: Budget | None = None,
    hitl: HITLPolicy | None = None,
    tokenizer_func: Callable[[str], int] | None = None,
) -> Callable[[F], F]:
    """
    A decorator that enforces execution budgets and HITL policies on a tool function.
    """

    def decorator(func: F) -> F:
        def _wrap_input_generator(
            gen: Generator[Any, Any, Any], arg_name: str = ""
        ) -> Generator[Any, Any, Any]:
            for item in gen:
                serialized_item = deep_serialize(item)
                if hitl:
                    hitl.evaluate_serialized(func.__name__, {arg_name: serialized_item})
                if budget and budget.max_tokens is not None:
                    if tokenizer_func:
                        try:
                            consumed = tokenizer_func(json.dumps(serialized_item))
                        except Exception:
                            consumed = estimate_tokens_safely(serialized_item)
                    else:
                        consumed = estimate_tokens_safely(serialized_item)
                    budget.consume_tokens(consumed)
                yield item

        def _wrap_input_async_generator(
            gen: AsyncGenerator[Any, Any], arg_name: str = ""
        ) -> AsyncGenerator[Any, Any]:
            async def wrapper() -> AsyncGenerator[Any, Any]:
                async for item in gen:
                    serialized_item = deep_serialize(item)
                    if hitl:
                        hitl.evaluate_serialized(func.__name__, {arg_name: serialized_item})
                    if budget and budget.max_tokens is not None:
                        if tokenizer_func:
                            try:
                                consumed = tokenizer_func(json.dumps(serialized_item))
                            except Exception:
                                consumed = estimate_tokens_safely(serialized_item)
                        else:
                            consumed = estimate_tokens_safely(serialized_item)
                        budget.consume_tokens(consumed)
                    yield item

            return wrapper()

        def _process_inputs(
            args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> tuple[tuple[Any, ...], dict[str, Any]]:
            new_args = list(args)
            for i, arg in enumerate(new_args):
                if inspect.isgenerator(arg):
                    new_args[i] = _wrap_input_generator(arg, arg_name=f"arg_{i}")
                elif inspect.isasyncgen(arg):
                    new_args[i] = _wrap_input_async_generator(arg, arg_name=f"arg_{i}")

            new_kwargs = dict(kwargs)
            for k, v in new_kwargs.items():
                if inspect.isgenerator(v):
                    new_kwargs[k] = _wrap_input_generator(v, arg_name=k)
                elif inspect.isasyncgen(v):
                    new_kwargs[k] = _wrap_input_async_generator(v, arg_name=k)

            return tuple(new_args), new_kwargs

        def _pre_execution(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            # Serialize ONCE at the boundary for both HITL and Budgeting
            sig = inspect.signature(func)
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()
            serialized_inputs = deep_serialize(bound_args.arguments)

            if hitl:
                # HITL Policy now accepts the pre-serialized dictionary
                hitl.evaluate_serialized(func.__name__, serialized_inputs)

            if budget and budget.max_tokens is not None:
                if tokenizer_func:
                    try:
                        consumed = tokenizer_func(json.dumps(serialized_inputs))
                    except Exception:
                        consumed = estimate_tokens_safely(serialized_inputs)
                else:
                    consumed = estimate_tokens_safely(serialized_inputs)
                budget.consume_tokens(consumed)

            if budget:
                budget.consume_call()

        def _consume_post_execution(result: Any) -> None:
            """Helper to consume tokens from standard results."""
            if budget and budget.max_tokens is not None:
                serialized = deep_serialize(result)
                if tokenizer_func:
                    try:
                        consumed = tokenizer_func(json.dumps(serialized))
                    except Exception:
                        consumed = estimate_tokens_safely(serialized)
                else:
                    consumed = estimate_tokens_safely(serialized)
                budget.consume_tokens(consumed)

        if inspect.isasyncgenfunction(func):

            @functools.wraps(func)
            async def async_gen_wrapper(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, Any]:
                processed_args, processed_kwargs = _process_inputs(args, kwargs)
                _pre_execution(processed_args, processed_kwargs)
                try:
                    async for item in func(*processed_args, **processed_kwargs):
                        _consume_post_execution(item)
                        yield item
                finally:
                    pass

            return cast(F, async_gen_wrapper)

        elif inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                processed_args, processed_kwargs = _process_inputs(args, kwargs)
                _pre_execution(processed_args, processed_kwargs)
                result = await func(*processed_args, **processed_kwargs)
                _consume_post_execution(result)
                return result

            return cast(F, async_wrapper)

        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                processed_args, processed_kwargs = _process_inputs(args, kwargs)
                _pre_execution(processed_args, processed_kwargs)
                result = func(*processed_args, **processed_kwargs)

                if inspect.isgenerator(result):

                    def gen_wrapper() -> Generator[Any, Any, Any]:
                        try:
                            for item in result:
                                _consume_post_execution(item)
                                yield item
                        finally:
                            pass

                    return gen_wrapper()

                _consume_post_execution(result)
                return result

            return cast(F, sync_wrapper)

    return decorator
