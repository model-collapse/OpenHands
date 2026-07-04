"""Top-level namespace package for OpenHands.

This package is declared as a namespace package so it can share the
``openhands`` top-level namespace with the ``openhands-sdk``,
``openhands-tools``, and ``openhands-agent-server`` dependencies. It also
re-exports ``__version__`` and ``get_version`` for backward compatibility.
"""

# This is a namespace package - extend the path to include installed packages
# (We need to do this to support dependencies openhands-sdk, openhands-tools and openhands-agent-server
# which all have a top level `openhands`` package.)
__path__ = __import__('pkgutil').extend_path(__path__, __name__)

# Import version information for backward compatibility
from openhands.app_server.version import __version__, get_version

__all__ = ['__version__', 'get_version']
