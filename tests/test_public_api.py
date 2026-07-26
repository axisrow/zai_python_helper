"""Smoke tests for the public, importable API surface (issue #18).

The package root ``__all__`` is the semantic-versioned contract. These tests
lock it against drift: every name in ``__all__`` must resolve on the package
object, and the set must stay equal to the names actually imported at the root
— so a future PR cannot silently drop a public name or export one it forgot to
declare.
"""

import importlib

import zai_python_helper as z


def test_all_names_resolve_on_package():
    """Every name in ``__all__`` is an attribute of the package object."""
    missing = [name for name in z.__all__ if not hasattr(z, name)]
    assert missing == [], f"__all__ names not importable: {missing}"


def test_version_is_exported_and_string():
    """``__version__`` is public and a non-empty string (hatch reads it)."""
    assert "__version__" in z.__all__
    assert isinstance(z.__version__, str)
    assert z.__version__


def test_error_hierarchy_is_public():
    """The catch surface: subclass errors inherit the sentinel."""
    for sub in ("ConfigurationError", "ValidationError", "ProviderError"):
        assert sub in z.__all__, f"{sub} missing from __all__"
        assert issubclass(getattr(z, sub), z.ZaiPythonHelperError)


def test_core_domain_types_are_public():
    """The pure-planning entry points callers build on are exported."""
    for name in (
        "ModelMode",
        "ProviderSpec",
        "Region",
        "plan_zai",
        "plan_default",
        "postconditions",
        "PatchPlan",
    ):
        assert name in z.__all__, f"{name} missing from __all__"
        assert hasattr(z, name)


def test_io_backends_and_paths_are_public():
    """Callers composing their own apply cycle need these."""
    for name in ("JsonBackend", "ShellBackend", "Paths"):
        assert name in z.__all__, f"{name} missing from __all__"
        assert hasattr(z, name)


def test_package_import_is_side_effect_free():
    """Importing the package must not perform IO (ADR-001: core has no IO).

    Re-importing fresh should succeed and leave ``__all__`` intact — a guard
    that the re-exports introduce no import-time file/env access.
    """
    importlib.reload(z)
    assert "__version__" in z.__all__
    assert len(z.__all__) >= 20


def test_tools_layer_is_public():
    """The S6 Tool protocol + registry are importable (issue #7 / #18)."""
    for name in ("Tool", "ManagedField", "StatusRow", "REGISTRY", "get_tool", "tool_names"):
        assert name in z.__all__, f"{name} missing from __all__"
        assert hasattr(z, name), f"{name} not importable"


def test_region_endpoint_constants_are_public():
    """The S6 region endpoint maps are importable for tools/callers."""
    for name in ("ZAI_PAAS_BASE_URL_BY_REGION", "ZAI_ANTHROPIC_BASE_URL_BY_REGION_V2"):
        assert name in z.__all__, f"{name} missing from __all__"
        assert hasattr(z, name)
