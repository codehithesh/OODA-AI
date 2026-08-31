"""Nodes package — pure async LangGraph node functions, one per file.

Every node:
* is a pure async function ``(state, config) -> partial state update``;
* validates its input and output through typed Pydantic models;
* performs NO database writes (all persistence happens in graph checkpointing
  or route handlers);
* documents its input state keys, output state keys, and side-effect
  guarantees in its docstring.
"""
