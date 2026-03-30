from pydantic import BaseModel
from beeai_framework.template import PromptTemplate


class _EmptySchema(BaseModel):
    pass


def system_prompt_template(text: str) -> PromptTemplate:
    """Create a PromptTemplate for a static system prompt (no variables)."""
    return PromptTemplate(schema=_EmptySchema, template=text)
