"""Errors intentionally exclude request bodies, passwords and server stack traces."""


class SmartBIError(Exception):
    """Base error for the SDK."""


class ConfigurationError(SmartBIError):
    pass


class AuthenticationError(SmartBIError):
    pass


class TransportError(SmartBIError):
    pass


class QueryTimeoutError(TransportError):
    pass


class ProtocolError(SmartBIError):
    pass


class ParameterError(SmartBIError):
    pass


class ResourceNotFoundError(SmartBIError):
    pass


class UnsupportedReportError(SmartBIError):
    pass


class ReportParseError(SmartBIError):
    pass


class ResourceLimitError(SmartBIError):
    pass


class ServiceBusyError(SmartBIError):
    pass


class QueryNotFoundError(SmartBIError):
    pass
