"""
Error types for zai_python_helper.

Per architecture, all errors inherit from ZaiPythonHelperError and
result in a one-line "error: <msg>" output with exit code 1.
"""


class ZaiPythonHelperError(Exception):
    """
    Base exception for all zai_python_helper errors.

    Per error contract (ARCHITECTURE.md), this results in:
    - One-line output: "error: <msg>"
    - Exit code: 1
    - Full traceback: only with --debug flag
    """

    def __init__(self, message: str):
        """
        Initialize the error with a message.

        Args:
            message: Human-readable error message
        """
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        """Return the error message."""
        return self.message


class ConfigurationError(ZaiPythonHelperError):
    """Error in configuration file or settings."""

    pass


class ValidationError(ZaiPythonHelperError):
    """Error in input validation."""

    pass


class ProviderError(ZaiPythonHelperError):
    """Error in provider configuration or connection."""

    pass
