"""Errors of the persistence layer. Messages carry names and codes only, never values."""


class PersistenceError(RuntimeError):
    """Storage failed or refused. The caller must fail closed: never answer from stale state and never report success."""


class VersionConflict(PersistenceError):
    """The case changed since this unit of work began (another process committed first). Nothing was written."""


class TenantScopeError(PersistenceError):
    """A lookup or write would have crossed a client boundary, or could not be scoped to one (GR-16)."""


class SchemaMismatchError(PersistenceError):
    """A row does not fit the table: an unknown column, or a required column left out."""


class UnsupportedParentStatus(PersistenceError):
    """The parent case is in a status UC-04 has no defined behaviour for (decision pending); it must not act."""
