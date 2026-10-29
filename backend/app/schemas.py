from typing import Literal

from pydantic import BaseModel, Field

Priority = Literal["urgent", "soon", "waiting", "healthy"]


class Analysis(BaseModel):
    """What the AI concludes about one account. Shown on the Today queue and the account brief."""

    priority: Priority = Field(
        description="urgent = needs action today/tomorrow; soon = should act within ~7 days; "
        "waiting = deliberately hold off (explicit timing/'don't push'); healthy = no action needed"
    )
    reason: str = Field(description="One sentence: why this priority, citing the concrete signal")
    next_action: str = Field(description="One concrete next step, imperative voice")
    contact_id: str | None = Field(description="Best person to contact for the next action (contact_xxx id)")
    due_date: str | None = Field(description="YYYY-MM-DD by which to do the next action")
    snooze_until: str | None = Field(description="YYYY-MM-DD; only when priority=waiting")
    summary: str = Field(description="2-3 sentence brief of where the relationship stands")
    good_to_know: list[str] = Field(
        description="Short facts worth remembering: questions or promises we owe them, risks, opportunities"
    )
    evidence: list[str] = Field(description="interaction ids (int_xxx) that support this assessment")


class HistorySummary(BaseModel):
    """Rolling summary of a client's older notes. Updated in batches, so prompts stay small."""

    summary: str = Field(description="3-5 sentences: how the relationship developed, oldest to newest")
    key_facts: list[str] = Field(
        description="Durable facts to keep: systems they use, who decides, promises, deadlines, requests, concerns"
    )


class ToolCall(BaseModel):
    """Ask's first step: the AI only chooses what to do. Code then does it."""

    tool: Literal["list_by_status", "find_clients", "answer", "draft_email"]
    priority: Priority | None = None   # list_by_status
    query: str | None = None           # find_clients, answer: what to search for
    client: str | None = None          # draft_email: practice or person named in the question
    instructions: str | None = None    # draft_email: topic or tone


class NewInteraction(BaseModel):
    contact_id: str | None = None
    type: Literal["email", "call", "meeting", "note"] = "note"
    occurred_at: str | None = None
    notes: str


class Question(BaseModel):
    question: str


class NewClient(BaseModel):
    name: str
    status: Literal["prospect", "customer"] = "prospect"
    contact_name: str
    contact_email: str
    contact_role: str = ""


class DraftRequest(BaseModel):
    instructions: str | None = None


class SendEmail(BaseModel):
    contact_id: str | None = None
    subject: str
    body: str
