# DEPRECATED: This module is deprecated. Use uvicorn directly:
#   uvicorn openhands.app_server.app:app --host 0.0.0.0 --port 3000
#
# This module is kept for backward compatibility.

import os

import uvicorn

from openhands.app_server.utils.logger import LOG_JSON, get_uvicorn_log_config


def main():
    """Run the OpenHands backend server via uvicorn.

    Serves the ``openhands.server.listen:app`` ASGI application, binding to
    host ``0.0.0.0``. The port defaults to ``3000`` and can be overridden with
    the ``port`` environment variable. The log level is ``debug`` when the
    ``DEBUG`` environment variable is set, otherwise ``info``. Logging uses the
    configuration returned by :func:`get_uvicorn_log_config`, and ANSI colors
    are disabled when JSON logging (``LOG_JSON``) is enabled.

    This module is a deprecated backward-compatibility entrypoint; prefer
    running uvicorn directly against ``openhands.app_server.app:app``.
    """
    log_config = get_uvicorn_log_config()

    uvicorn.run(
        'openhands.server.listen:app',
        host='0.0.0.0',
        port=int(os.environ.get('port') or '3000'),
        log_level='debug' if os.environ.get('DEBUG') else 'info',
        log_config=log_config,
        use_colors=False if LOG_JSON else None,
    )


if __name__ == '__main__':
    main()
