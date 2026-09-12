from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Evidence(Strict):
    passage_id: str
    quote: str = Field(min_length=1)

class Block(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    heading: str = Field(max_length=200)
    content: str = Field(min_length=1,max_length=16000)
    kind: Literal["Sourced", "Inference", "Uncertain", "Conflicting", "Human note"] = "Sourced"
    evidence: list[Evidence] = Field(default_factory=list,max_length=20)

class Relation(Strict):
    target_id: str
    type: Literal["RELATED_TO", "PART_OF", "DEPENDS_ON", "COMPETES_WITH", "USED_BY", "SOLVES"] = "RELATED_TO"

class Document(Strict):
    title: str = Field(min_length=1,max_length=200)
    aliases: list[str] = Field(default_factory=list,max_length=20)
    tags: list[str] = Field(default_factory=list,max_length=20)
    blocks: list[Block] = Field(default_factory=list,max_length=100)
    related: list[Relation] = Field(default_factory=list,max_length=30)

class PlanAction(Strict):
    type: Literal["CREATE_PAGE", "UPDATE_PAGE", "NO_CHANGE"]
    page_id: str | None = None
    expected_revision: str | None = None
    title: str = ""
    reason: str
    section_ids: list[str] = Field(default_factory=list)

class Plan(Strict):
    actions: list[PlanAction] = Field(max_length=12)

class Change(Strict):
    page_id: str | None = None
    expected_revision: str | None = None
    title: str
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    blocks: list[Block]
    related: list[Relation] = Field(default_factory=list)
    reason: str

class Proposal(Strict):
    changes: list[Change] = Field(max_length=12)

class ResearchResult(Strict):
    blocks: list[Block] = Field(min_length=1,max_length=20)

class Finding(Strict):
    page_id: str
    kind: str
    message: str

class AuditResult(Strict):
    findings: list[Finding] = Field(default_factory=list,max_length=50)

