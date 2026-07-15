"""The MCP adapter modules must import with the stripe package absent.

stripe is configured only behind ``if effective_billing_config is not None:``;
when it is not installed, billing is unconfigured and no stripe code runs. The
only thing that could break a stripe-free boot is a module-level ``import
stripe``. This test blocks stripe from sys.modules and reloads the three
adapter modules to prove none of them import stripe at module level.
"""

from __future__ import annotations

import importlib
import sys

import daimon.adapters.mcp.checkout as checkout
import daimon.adapters.mcp.server as server
import daimon.adapters.mcp.webhooks as webhooks


def test_mcp_adapter_modules_import_when_stripe_package_absent() -> None:
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "stripe" or name.startswith("stripe.")
    }

    # A None value in sys.modules makes a subsequent `import X` raise ImportError,
    # simulating the package being absent without uninstalling it.
    sys.modules["stripe"] = None  # type: ignore[assignment]
    sys.modules["stripe._http_client"] = None  # type: ignore[assignment]
    sys.modules["stripe.params.checkout._session_create_params"] = None  # type: ignore[assignment]

    try:
        importlib.reload(server)
        importlib.reload(webhooks)
        importlib.reload(checkout)
    except ImportError as err:
        raise AssertionError(
            "MCP adapter modules must import with stripe absent from sys.modules, "
            f"but reload raised ImportError: {err}"
        ) from err
    finally:
        for name in (
            "stripe",
            "stripe._http_client",
            "stripe.params.checkout._session_create_params",
        ):
            sys.modules.pop(name, None)
        sys.modules.update(saved)
        # Restore the three modules to their normal (stripe-installed) state so
        # subsequent tests in the same session see a clean import.
        importlib.reload(server)
        importlib.reload(webhooks)
        importlib.reload(checkout)
