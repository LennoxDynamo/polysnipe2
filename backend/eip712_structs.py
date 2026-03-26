"""
eip712_structs.py — Compatibility shim for py-clob-client on Windows.

py-clob-client imports `eip712_structs` which has no pre-built Windows wheels
and requires a C compiler. This shim redirects all imports to `poly_eip712_structs`,
a maintained fork with pre-built wheels for all platforms.

Place this file in the same directory as main.py (i.e. backend/).
Python's import system will find it before looking for the missing package.
"""

try:
    # Try the original first (works on Linux/Mac with compiler)
    from eip712_structs import *  # noqa: F401, F403
    from eip712_structs import (  # noqa: F401
        EIP712Message, EIP712Struct, Address, Array, Boolean,
        Bytes, Int, String, Uint, make_domain,
    )
except ImportError:
    # Fall back to the Windows-compatible fork
    from poly_eip712_structs import *  # noqa: F401, F403
    from poly_eip712_structs import (  # noqa: F401
        EIP712Message, EIP712Struct, Address, Array, Boolean,
        Bytes, Int, String, Uint, make_domain,
    )
