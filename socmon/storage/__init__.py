"""Storage backends. Pick one in config; both implement `Storage` from `.base`."""

from socmon.storage.base import Storage

__all__ = ["Storage", "get_storage"]


def get_storage(backend: str, dsn: str) -> Storage:
    """Factory. Postgres is opt-in via the `postgres` extra."""
    if backend == "sqlite":
        from socmon.storage.sqlite import SqliteStorage
        return SqliteStorage(dsn)
    if backend == "postgres":
        from socmon.storage.sqlite import SqliteStorage  # SQLAlchemy DSN works for both
        return SqliteStorage(dsn)
    raise ValueError(f"unknown storage backend: {backend}")
