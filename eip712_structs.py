"""Top-level compatibility shim for py-clob-client.

Railway runs the app from repository root (`uvicorn backend.main:app`), while
py-clob-client imports `eip712_structs` as a top-level module.
This file re-exports the backend shim so the import always resolves.
"""

from backend.eip712_structs import *  # noqa: F401, F403
