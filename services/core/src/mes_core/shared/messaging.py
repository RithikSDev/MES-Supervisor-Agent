from mes_common.messaging import Messaging
from mes_core.shared.db.base import Base

# Outbox/inbox tables live in the `messaging` schema of the core database.
messaging = Messaging(Base.metadata, source="mes-core")
