"""
Paths dataclass for configuration file locations.

This is a stub implementation. Full implementation per ADR-001/004/005
is tracked in epic #1.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """
    Configuration file paths for zai_python_helper.

    Per ADR-001, this is an IO-free dataclass that represents
    the locations of configuration files. Resolution from
    environment variables is handled in the IO layer.
    """

    claude_settings: Path
    claude_json: Path
    zshrc: Path
    ownership_json: Path
    lock_file: Path
    state_dir: Path

    @staticmethod
    def from_home(home: Path | str) -> "Paths":
        """
        Create Paths instance from a home directory.

        Args:
            home: Home directory path

        Returns:
            Paths instance with all fields resolved
        """
        home_path = Path(home)
        return Paths(
            claude_settings=home_path / ".claude" / "settings.json",
            claude_json=home_path / ".claude.json",
            zshrc=home_path / ".zshrc",
            ownership_json=home_path / ".zai-python-helper" / "ownership.json",
            lock_file=home_path / ".zai-python-helper" / "lock",
            state_dir=home_path / ".zai-python-helper" / "state",
        )

    @staticmethod
    def default() -> "Paths":
        """
        Create Paths instance using the current home directory.

        Returns:
            Paths instance using Path.home()
        """
        return Paths.from_home(Path.home())
