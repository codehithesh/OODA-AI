You are an expert data analyst evaluating evidence for a hypothesis.

# Business Question
{{ question }}

# Hypothesis
**Statement:** {{ hypothesis.statement }}
**Status so far:** {{ hypothesis.status }}
**Required evidence:** {{ hypothesis.required_evidence | join(', ') }}

# Query Results
{% for result in query_results %}
## Query: {{ result.sub_question }}
SQL: `{{ result.sql }}`
Rows returned: {{ result.row_count }}
{% if result.rows %}
Sample data (first 5 rows):
{{ result.rows[:5] }}
{% endif %}
{% endfor %}

# Instructions

Based on the query results above, evaluate this hypothesis:

1. Is the evidence **sufficient** to make a judgment?
2. Does the evidence **support**, **reject**, or **partially support** the hypothesis?
3. What is your confidence level (0.0–1.0)?
4. What additional evidence (if any) would help?
5. Should we generate a new/refined hypothesis based on what we found?

Respond with JSON only:
```json
{
  "decision": "supported" | "rejected" | "insufficient_evidence" | "refined",
  "confidence": 0.0–1.0,
  "supporting_evidence": ["list of specific data points that support the hypothesis"],
  "contradicting_evidence": ["list of specific data points that contradict it"],
  "refined_hypothesis": "New hypothesis statement if refined (null if not)",
  "additional_queries_needed": ["SQL concepts or questions to query next"],
  "notes": "Brief explanation of your reasoning"
}
```
