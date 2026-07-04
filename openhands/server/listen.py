"""ASGI entrypoint for the OpenHands backend server.

This module exposes the FastAPI ``app`` used to run the backend (for example via
``uvicorn openhands.server.listen:app``, as invoked by ``make start-backend``).
It is retained for backward compatibility and simply re-exports the application
from :mod:`openhands.app_server.app`.
"""

# DEPRECATED: This module is deprecated and will be removed in a future release.
# Please use openhands.app_server.app instead.
#
# For backward compatibility, this module re-exports the app from openhands.app_server.app.

from openhands.app_server.app import app

__all__ = ['app']
