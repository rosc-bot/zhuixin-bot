"""Backward-compatible import shim for the canonical library service.

The implementation lives only in :mod:`services.library_service`.  Keeping this
module as a re-export avoids breaking maintenance scripts while preventing two
implementations from drifting apart.
"""

from services.library_service import *
