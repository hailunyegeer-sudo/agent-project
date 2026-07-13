"""Decorator that attaches tool metadata to Skill functions for auto-generating tools_schema."""

from __future__ import annotations

from typing import Any, Callable


def tool(
    description: str,
    parameters: dict[str, str],
    returns: dict[str, dict[str, str]],
) -> Callable:
    """
    Decorate a Skill function with metadata for tools_schema generation.

    ``description`` is the human-readable tool description shown to the LLM.

    ``parameters`` maps each user-facing parameter name to its description.
    Parameter *types* are extracted from type hints at schema-generation time,
    so they do not need to be declared here.

    ``returns`` maps each return field to ``{"type": "<json_type>", "description": "..."}``.
    Valid JSON types: string, integer, number, boolean, array, object.

    Example::

        @tool(
            description="Calculate a safe arithmetic expression.",
            parameters={"expression": "Arithmetic expression using + - * / etc."},
            returns={"result": {"type": "number", "description": "Calculated value."}},
        )
        def calculator(expression: str) -> dict:
            ...
    """

    def decorator(func: Callable) -> Callable:
        func.__tool_meta__ = {  # type: ignore[attr-defined]
            "description": description,
            "parameters": parameters,
            "returns": returns,
        }
        return func

    return decorator


def get_tool_meta(func: Callable) -> dict[str, Any] | None:
    """Return ``__tool_meta__`` from a decorated function, or None."""
    return getattr(func, "__tool_meta__", None)
