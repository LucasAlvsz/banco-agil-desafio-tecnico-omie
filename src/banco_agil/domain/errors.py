class DomainError(Exception):
    pass


class DomainValidationError(DomainError, ValueError):
    pass


class ClientNotFoundError(DomainError):
    pass


class PersistenceError(DomainError):
    pass
