"""Secret resolution and redaction utilities.

Provides functions to resolve API keys from command-line flags, environment
variables, or interactive prompts, following the flag → env → prompt chain.
Also provides redaction for safe logging of sensitive values.
"""

import getpass
import os


def resolve_key(
    flag_value: str | None,
    env_var: str = "ZAI_API_KEY",
    prompt_message: str = "Enter Z.ai API key: ",
) -> str:
    """Resolve an API key from flag → env → prompt.

    Args:
        flag_value: The value from the command-line flag (highest priority).
        env_var: The environment variable name to check.
        prompt_message: The message to show when prompting (lowest priority).

    Returns:
        The resolved API key.

    Raises:
        ZaiPythonHelperError: If no key can be resolved (e.g., EOF on prompt).
    """
    from zai_python_helper.errors import ZaiPythonHelperError

    # 1. Check flag value (highest priority)
    if flag_value:
        return flag_value

    # 2. Check environment variable
    env_value = os.environ.get(env_var)
    if env_value:
        return env_value

    # 3. Prompt interactively (lowest priority)
    try:
        return getpass.getpass(prompt_message)
    except EOFError as e:
        raise ZaiPythonHelperError("No API key provided (EOF on prompt)") from e


def redact(value: str, visible_chars: int = 4, mask_char: str = "*") -> str:
    """Redact a sensitive value for safe logging.

    Shows the first ``visible_chars`` characters and masks the rest.

    Args:
        value: The sensitive value to redact.
        visible_chars: Number of characters to show at the start.
        mask_char: The character to use for masking.

    Returns:
        The redacted value (e.g., ``"sk-***"`` for ``"sk-12345678"`` with
        ``visible_chars=3``).
    """
    if len(value) <= visible_chars:
        return value  # Too short to redact meaningfully
    return f"{value[:visible_chars]}{mask_char * (len(value) - visible_chars)}"
