"""Tool registry — first-class tool calling architecture.

Every tool the agent can invoke is registered here.  Tools are typed units of
work with a structured input schema, structured output schema, validation,
error handling, timing, and optional cost attribution.

Usage::

    from tools import registry
    tool = registry.get("execute_sql")
    result = await tool.run({"sql": "SELECT 1"})

"""
