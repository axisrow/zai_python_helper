"""
core/planner — Pure planning functions (v1 domain).

Per ADR-001, this module contains PURE functions that transform
parsed config documents into PatchPlans. No IO, no env access, no file operations.

The planner is responsible for:
- Understanding the structure of tool config files
- Generating deltas (PatchPlans) to achieve desired state
- Validating postconditions
"""
