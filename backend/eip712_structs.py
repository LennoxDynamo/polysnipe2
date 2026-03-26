"""Compatibility shim for py-clob-client on Windows.

The upstream `eip712-structs` package typically requires compilation,
while `poly-eip712-structs` provides Windows wheels. We expose the same
import surface used by py-clob-client.
"""

from poly_eip712_structs import *  # noqa: F401, F403
from poly_eip712_structs import (  # noqa: F401
    Address,
    Array,
    Boolean,
    Bytes,
    EIP712Struct,
    Int,
    String,
    Uint,
    make_domain,
)

# Compatibility alias: some libraries refer to EIP712Message.
EIP712Message = EIP712Struct
