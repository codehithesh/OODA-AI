"""Jinja2 prompt renderer for ``context/prompts/*.md``.

All agent prompts live as Jinja2 markdown templates under the git-versioned
context directory. Nodes render them through this loader; nothing else in the
codebase contains prompt text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from config import get_settings


class PromptLoader:
    """Renders Jinja2 ``.md`` prompt templates from a prompts directory."""

    def __init__(self, prompts_dir: Path) -> None:
        self._env = Environment(
            loader=FileSystemLoader(str(prompts_dir)),
            autoescape=False,  # prompts are markdown, not HTML
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
            undefined=StrictUndefined,  # surface missing variables at development time
        )
        self.prompts_dir = prompts_dir

    def render(self, template_name: str, **variables: Any) -> str:
        """Render ``template_name`` (e.g. 'analytics/generate_sql.md') with vars."""
        template = self._env.get_template(template_name)
        return template.render(**variables)

    def list_templates(self) -> list[str]:
        """All prompt template paths available under the prompts directory."""
        return sorted(self._env.loader.list_templates()) if self._env.loader else []


_default_loader: PromptLoader | None = None


def get_default_prompt_loader() -> PromptLoader:
    """Process-wide PromptLoader pointed at ``settings.context_path/prompts``."""
    global _default_loader
    if _default_loader is None:
        s = get_settings()
        _default_loader = PromptLoader(s.context_path / "prompts")
    return _default_loader


def get_prompt_loader(config: dict[str, Any] | None = None) -> PromptLoader:
    """Loader for this node run: config override first, then the default."""
    if config is not None:
        configurable = config.get("configurable") or {}
        override = configurable.get("prompt_loader")
        if override is not None:
            return override
    return get_default_prompt_loader()
