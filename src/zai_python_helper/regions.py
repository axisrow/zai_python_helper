"""Region configuration and endpoint mapping.

Provides the ``Region`` enum and endpoint matrix for different Z.ai
deployment environments (global/china × anthropic/paas). This is a pure
domain module with no side effects.
"""

from enum import Enum


class Region(Enum):
    """Z.ai deployment region."""

    GLOBAL = "global"
    CHINA = "china"


def get_endpoint(region: Region, service: str) -> str:
    """Get the API endpoint for a given region and service.

    Args:
        region: The deployment region.
        service: The service type ("anthropic" or "paas").

    Returns:
        The base URL for the endpoint.

    Raises:
        ZaiPythonHelperError: If the region/service combination is invalid.
    """
    from zai_python_helper.errors import ZaiPythonHelperError

    endpoints = {
        Region.GLOBAL: {
            "anthropic": "https://api.anthropic.com",
            "paas": "https://api.zai.ai",
        },
        Region.CHINA: {
            "anthropic": "https://api.anthropic.cn",
            "paas": "https://api.zai.cn",
        },
    }

    try:
        return endpoints[region][service]
    except KeyError as e:
        raise ZaiPythonHelperError(
            f"Invalid region/service combination: {region.value}/{service}"
        ) from e
