You are an expert analyst synthesizing internal company data with external market context.

# Business Question
{{ question }}

# Internal Company Data (Warehouse Results)
{{ internal_summary }}

# External Market Context (Web Research)
{% for search in web_searches %}
## Search: {{ search.query }}
{% for result in search.results %}
- **{{ result.title }}**: {{ result.snippet }}
{% endfor %}
{% endfor %}

# Instructions

Your task is to **fuse** these two sources of context into a unified analytical view.

Important rules:
1. Do NOT directly compare internal metrics with external benchmarks unless time periods, geographies, and metric definitions are compatible.
2. Flag any comparability issues explicitly.
3. Distinguish clearly between internal evidence and external evidence.
4. Identify insights that only emerge from combining both sources.

Respond with JSON only:
```json
{
  "internal_summary": "2–3 sentence summary of what the internal data shows",
  "external_summary": "2–3 sentence summary of the external context",
  "comparability_issues": ["any issues that make direct comparison problematic"],
  "internal_evidence": ["key facts from internal data"],
  "external_evidence": ["key facts from external research"],
  "combined_insights": ["insights that emerge from combining both sources"],
  "fusion_notes": "Any important caveats about the fusion process"
}
```
