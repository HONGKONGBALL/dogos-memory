"""Typed boundaries for profiles, observations, and retrieved memory."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError
from typing_extensions import Self, assert_never, override

DogId = NewType("DogId", str)
MemoryId = NewType("MemoryId", str)
SessionId = NewType("SessionId", str)
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
Identifier = StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
Score = Annotated[int, Field(strict=True, ge=0, le=100)]
Timestamp = Annotated[int, Field(strict=True, ge=0, le=9_223_372_036_854_775_807)]


class BoundaryModel(BaseModel):
    """Immutable, extra-field-rejecting input/output boundary."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class Profile(BoundaryModel):
    """Fixed owner and demo rules for one database file."""

    dog_id: Annotated[DogId, Identifier]
    name: Text
    personality: Text
    familiar_threshold: Annotated[int, Field(strict=True, ge=1, le=100)]
    mode: Literal["live", "simulation"] = "live"


class MemoryInput(BoundaryModel):
    """One terminal observation or evidence-backed, non-factual impression."""

    memory_id: Annotated[MemoryId, Identifier]
    session_id: Annotated[SessionId, Identifier]
    peer_id: Annotated[DogId, Identifier]
    kind: Literal["event", "chat", "thought"]
    event_type: Text
    content: Text
    occurred_at_ms: Timestamp
    source: Literal["robot_feedback", "operator", "llm", "simulation"]
    result: Literal["completed", "failed", "unconfirmed", "derived"]
    importance: Annotated[int, Field(strict=True, ge=1, le=10)] = 5
    affinity_delta: Score = 0
    evidence_ids: tuple[Annotated[MemoryId, Identifier], ...] = ()

    @model_validator(mode="after")
    def check_memory_contract(self) -> Self:
        """Keep facts, beliefs, and relationship scoring distinct."""
        match self.kind:
            case "thought":
                valid = (
                    self.result == "derived"
                    and self.source in {"llm", "operator"}
                    and self.affinity_delta == 0
                    and bool(self.evidence_ids)
                )
            case "event" | "chat":
                valid = (
                    self.result != "derived"
                    and self.source != "llm"
                    and not self.evidence_ids
                    and (
                        self.affinity_delta == 0
                        or (self.kind == "event" and self.result == "completed")
                    )
                )
            case _:
                assert_never(self.kind)
        if not valid or len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise PydanticCustomError("memory_contract", "Invalid fact/thought/evidence contract")
        return self


class StoredMemory(MemoryInput):
    """Persisted memory with a separate ingestion timestamp."""

    recorded_at_ms: Timestamp


class RecallRequest(BoundaryModel):
    """Bounded exact-peer retrieval; no semantic search is claimed."""

    peer_id: Annotated[DogId, Identifier]
    limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 3


class RecallResult(BoundaryModel):
    """Context ready for the rule engine, dashboard, or optional LLM."""

    profile: Profile
    peer_id: DogId
    affinity: Score
    familiar: bool
    facts: tuple[StoredMemory, ...]
    thoughts: tuple[StoredMemory, ...]


class AppendResult(BoundaryModel):
    """Idempotent append outcome and current relationship score."""

    memory_id: MemoryId
    inserted: bool
    affinity: Score


class StoreError(RuntimeError):
    """Actionable storage failure; SQL exceptions retain their cause."""

    code: str
    detail: str

    def __init__(self, code: str, detail: str) -> None:
        """Keep a stable machine code separate from diagnostic detail."""
        self.code = code
        self.detail = detail
        super().__init__(code, detail)

    @override
    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"
