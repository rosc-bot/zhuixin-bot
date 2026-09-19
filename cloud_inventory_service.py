"""Backward-compatible import shim for the canonical cloud inventory service.

The implementation lives only in :mod:`services.cloud_inventory_service`.  This
module preserves legacy script imports without maintaining a second copy.
"""

from services.cloud_inventory_service import *
