"""Role-addressed access to the two independent memory stores."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, final

from dogos_demo.models import DemoConfig, DemoError, DogRole
from dogos_demo.policy import PairRecalls, dog_with_role
from dogos_demo.session_models import DogMemoryView, PairSnapshot
from dogos_memory.models import DogId, RecallRequest, RecallResult, SessionId, StoredMemory

if TYPE_CHECKING:
    from dogos_demo.persistence import StoreBinding
    from dogos_memory.store import MemoryStore

COMMAND_ID_PARTS: Final = 4


@final
class PairMemory:
    """Read two stores consistently by configured role and owner identity."""

    def __init__(
        self,
        config: DemoConfig,
        bindings: tuple[StoreBinding, StoreBinding],
    ) -> None:
        """Bind the validated pair configuration to its two stores."""
        self._config = config
        self._bindings = bindings

    def recalls(self) -> PairRecalls:
        """Fetch bounded exact-peer context for both dogs."""
        cautious = dog_with_role(self._config, DogRole.CAUTIOUS)
        outgoing = dog_with_role(self._config, DogRole.OUTGOING)
        return PairRecalls(
            cautious=self.store(cautious.dog_id).recall(
                RecallRequest(peer_id=outgoing.dog_id, limit=20)
            ),
            outgoing=self.store(outgoing.dog_id).recall(
                RecallRequest(peer_id=cautious.dog_id, limit=20)
            ),
        )

    def snapshot(self) -> PairSnapshot:
        """Build the small dashboard projection from fresh recall results."""
        recalls = self.recalls()
        return PairSnapshot(
            mode=self._config.mode,
            cautious=_view(recalls.cautious),
            outgoing=_view(recalls.outgoing),
        )

    def store(self, dog_id: DogId) -> MemoryStore:
        """Resolve exactly one owner-bound store."""
        binding = next((item for item in self._bindings if item.dog.dog_id == dog_id), None)
        if binding is None:
            raise DemoError("store_binding_missing", str(dog_id))
        return binding.store

    def original_initiator(self, session_id: SessionId) -> DogId | None:
        """Recover step-one actor from stable completed-memory IDs."""
        memories = tuple(
            memory
            for binding in self._bindings
            for memory in binding.store.list_session(session_id)
            if _is_action_completion(memory)
        )
        if not memories:
            return None
        actors = {
            actor
            for memory in memories
            for actor in (_step_one_actor(memory, session_id),)
            if actor is not None
        }
        if len(actors) != 1:
            raise DemoError("session_plan_conflict", str(session_id))
        actor = next(iter(actors))
        if self._config.find_dog(actor) is None:
            raise DemoError("session_actor_not_configured", str(actor))
        return actor


def _view(recall: RecallResult) -> DogMemoryView:
    return DogMemoryView(
        dog_id=recall.profile.dog_id,
        peer_id=recall.peer_id,
        affinity=recall.affinity,
        familiar=recall.familiar,
        latest_memory_id=recall.facts[0].memory_id if recall.facts else None,
    )


def _is_action_completion(memory: StoredMemory) -> bool:
    return memory.event_type == "action_completed" and memory.result == "completed"


def _step_one_actor(memory: StoredMemory, session_id: SessionId) -> DogId | None:
    parts = str(memory.memory_id).rsplit("/", 3)
    if len(parts) != COMMAND_ID_PARTS or parts[0] != session_id or parts[2:] != ["1", "completed"]:
        return None
    return DogId(parts[1])
