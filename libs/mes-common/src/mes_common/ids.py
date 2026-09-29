import uuid


def new_id() -> uuid.UUID:
    """Time-ordered UUIDv7: index-friendly primary keys that still sort by creation time."""
    return uuid.uuid7()
