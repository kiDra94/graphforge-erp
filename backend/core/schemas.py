"""Cross-domain schemas shared by more than one router."""

from pydantic import BaseModel, ConfigDict, Field


class InputModel(BaseModel):
    """Base of every schema that accepts a request body.

    Carries nothing but `extra="forbid"` and not a single field: an unknown key in the
    body therefore produces a 422 that names it, instead of being dropped in silence.

    **Why this is a class and not a repeated line.** Pydantic discards unknown keys by
    default. That is exactly how a misspelled field name goes unnoticed: the value is
    lost on save and comes back as `undefined` on load, without an error reaching
    anyone. A configuration line per schema would have the same effect, but a schema
    added later can forget it. Inheritance cannot be forgotten — anything deriving from
    `BaseModel` instead stands out in review.

    **Write models only.** The read models stay deliberately tolerant: they map a graph
    that has grown over time, in which nodes carry properties no current schema knows
    about. Tightening them would turn an already stored database state into an HTTP 500.

    **No violation of the substitution principle.** Unlike a shared field base, this
    class changes not a single field type; it only sets a configuration. Write models
    keep declaring their fields independently.

    **Interaction with an own `model_config`.** Pydantic merges the configuration along
    the inheritance chain. A derived schema setting, say, `json_schema_extra` keeps
    `extra="forbid"` from this class — only what it declares under the same key wins.
    """
    model_config = ConfigDict(extra="forbid")


class ErrorResponse(BaseModel):
    """Global schema for standardised error messages.

    Used by every router across all domains for error responses.
    """
    message: str = Field(description="A human-readable error description for the client.")
