"""
io — IO layer for zai_python_helper.

Per ADR-001, this module handles all IO operations:
- File reading/writing (atomic)
- Environment variable resolution
- Network operations (for v2 proxy)

Core logic lives in core/ and remains pure.
"""
