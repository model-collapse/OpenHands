"""Top-level package for OpenHands.

This is configured as a namespace package so that the ``openhands`` namespace
can be shared across the separately installed ``openhands-sdk``,
``openhands-tools`` and ``openhands-agent-server`` distributions, which each
provide their own subpackages under the same top-level ``openhands`` package.

It re-exports the package version (``__version__`` and ``get_version``) from
``openhands.app_server.version`` for backward compatibility.
"""

# This is a namespace package - extend the path to include installed packages
# (We need to do this to support dependencies openhands-sdk, openhands-tools and openhands-agent-server
# which all have a top level `openhands`` package.)
__path__ = __import__('pkgutil').extend_path(__path__, __name__)

# Import version information for backward compatibility
from openhands.app_server.version import __version__, get_version

__all__ = ['__version__', 'get_version']
