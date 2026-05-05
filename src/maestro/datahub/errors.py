class DataHubError(Exception):
    """Base error for DataHub routing and provider failures."""


class UnsupportedDataTypeError(DataHubError):
    """Raised when a request asks for a data type DataHub does not recognize."""


class NoProviderError(DataHubError):
    """Raised when no registered provider can satisfy a request."""


class ProviderUnavailableError(DataHubError):
    """Raised when matching providers exist but are unavailable."""


class StaleDataError(DataHubError):
    """Raised when stale data is not allowed for a routed request."""
