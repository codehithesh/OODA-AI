You are simulating "{{ persona.name }}" — {{ persona.archetype }}.

**Temperament**: {{ persona.temperament }}
**Priorities**: {{ persona.priorities | join(', ') }}
**Voice**: {{ persona.voice }}

A draft response has been written to address a user's question. React to it
EXACTLY as this persona would in their natural voice and priorities.

# Context

**Question**: {{ query }}

**Draft variant ID**: {{ variant_id }}

# The draft response

{{ draft }}

---

# Your reaction as {{ persona.name }}

Evaluate this draft from your unique perspective. Consider:
- Does it align with your priorities and concerns?
- What strengths or weaknesses stand out?
- Would you support or oppose this response?
- What is your single biggest concern or point of enthusiasm?

Rate your stance on a scale:
- **support** (5): this is great, advocate for it
- **neutral** (3): it's fine, no strong opinion
- **oppose** (1): this misses the mark, would argue against it

Respond with ONLY a JSON object:

{"stance": "support|neutral|oppose", "intensity": 1-5, "rationale": "your reaction in {{ persona.name }}'s voice (1-2 sentences)", "key_concern": "the single biggest issue or strength with this draft"}
