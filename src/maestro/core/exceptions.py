class MaestroError(Exception):
    """Base exception for Maestro v0.1."""


class PluginLoadError(MaestroError):
    pass


class ValidationError(MaestroError):
    pass


class RiskError(MaestroError):
    pass


class MissingPriceError(MaestroError):
    pass
