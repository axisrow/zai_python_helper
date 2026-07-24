"""
core — Pure domain logic.

This module contains shared domain types and pure functions that implement
the core logic of zai_python_helper. Per ADR-001, nothing in core/ performs
IO — no file operations, no network calls, no environment variable access.

Core is split into:
- domain.py: Shared domain types (used by both planner and router)
- planner/: Pure functions for config file planning (v1 domain)
"""
