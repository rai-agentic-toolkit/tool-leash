import json
from collections.abc import Callable
from typing import Any

from .exceptions import CallBlockedError
from .serialization import deep_search_dict


class CallGuard:
    """Evaluates whether a tool call should be blocked based on argument patterns."""

    def __init__(
        self,
        restricted_args: dict[str, list[str]] | None = None,
        custom_validator: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.restricted_args = restricted_args or {}
        self.custom_validator = custom_validator

    def evaluate_serialized(self, func_name: str, serialized_args: dict[str, Any]) -> None:
        """
        Evaluate if the PRE-SERIALIZED provided arguments trigger any guard policies.
        Raises CallBlockedError if a rule is triggered.
        """
        # 1. Custom validator (Top Priority)
        if self.custom_validator:
            self.custom_validator(serialized_args)

        # 2. Key-Targeted Recursive Validation
        for target_arg_name, forbidden_substrings in self.restricted_args.items():
            # Recursively hunt for the targeted key anywhere in the payload (catches **kwargs nesting)
            found_values = deep_search_dict(serialized_args, target_arg_name)

            for matched_value in found_values:
                # Convert the specific discovered value to a JSON string for flat searching
                val_str = (
                    json.dumps(matched_value)
                    if isinstance(matched_value, dict | list)
                    else str(matched_value)
                )

                for substring in forbidden_substrings:
                    if substring in val_str:
                        raise CallBlockedError(
                            message=f"Call blocked: Tool '{func_name}' argument '{target_arg_name}' contains restricted substring '{substring}'.",
                            tool_name=func_name,
                            trigger_reason=f"Matched restricted substring '{substring}' in resolved argument '{target_arg_name}': {val_str}",
                        )
