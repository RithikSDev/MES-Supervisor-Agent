from dataclasses import dataclass
from uuid import UUID

from mes_core.shared.db.context import Actor
from mes_core.shared.errors import Forbidden
from mes_core.shared.permissions import Perm


@dataclass(frozen=True)
class CurrentUser:
    id: UUID
    username: str
    display_name: str | None
    roles: frozenset[str]
    permissions: frozenset[Perm]
    # None = every plant (admin). Otherwise the plants the user is scoped to.
    plant_ids: frozenset[UUID] | None
    # Set when the request comes from an operator PIN session on a shop-floor terminal.
    terminal_id: UUID | None = None
    terminal_session_id: UUID | None = None

    @property
    def actor(self) -> Actor:
        return Actor(id=str(self.id), name=self.username, type="USER")

    def has(self, perm: Perm) -> bool:
        return perm in self.permissions

    def can_access_plant(self, plant_id: UUID) -> bool:
        return self.plant_ids is None or plant_id in self.plant_ids

    def ensure_plant(self, plant_id: UUID) -> None:
        if not self.can_access_plant(plant_id):
            raise Forbidden("you are not assigned to this plant")
