import contextlib
import os
import warnings

from fastapi.routing import Mount

with warnings.catch_warnings():
    warnings.simplefilter('ignore')

from fastapi import (
    FastAPI,
    Request,
)
from fastapi.responses import JSONResponse

from openhands.app_server import v1_router
from openhands.app_server.config import get_app_lifespan_service
from openhands.app_server.integrations.service_types import AuthenticationError
from openhands.app_server.mcp.mcp_router import init_tavily_proxy, mcp_server
from openhands.app_server.middleware import (
    CacheControlMiddleware,
    InMemoryRateLimiter,
    LocalhostCORSMiddleware,
    RateLimitMiddleware,
)
from openhands.app_server.static import SPAStaticFiles
from openhands.app_server.status.status_router import router as health_router
from openhands.app_server.version import get_version

# Initialize the Tavily MCP proxy before creating the app
init_tavily_proxy()

mcp_app = mcp_server.http_app(path='/mcp', stateless_http=True)


def combine_lifespans(*lifespans):
    # Create a combined lifespan to manage multiple session managers
    @contextlib.asynccontextmanager
    async def combined_lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            for lifespan in lifespans:
                await stack.enter_async_context(lifespan(app))
            yield

    return combined_lifespan


lifespans = [mcp_app.lifespan]
app_lifespan_ = get_app_lifespan_service()
if app_lifespan_:
    lifespans.append(app_lifespan_.lifespan)

# Optional GitHub poller: an in-process service that polls GitHub and starts
# conversations on issue/comment triggers. Opt-in via ENABLE_GITHUB_POLLER so
# it is inert in tests and deployments that don't want it. Accepts 'true'/'1'.
_github_poller_enabled = os.getenv('ENABLE_GITHUB_POLLER', 'false').lower() in (
    'true',
    '1',
)
if _github_poller_enabled:
    from openhands.app_server.github_poller.service import get_github_poller_service

    _github_poller = get_github_poller_service()

    @contextlib.asynccontextmanager
    async def _github_poller_lifespan(app):
        async with _github_poller:
            yield

    lifespans.append(_github_poller_lifespan)

# Optional AI team: a supervised multi-agent engineering team built on the
# internal issue store (see docs/design/ai-team*.md). Opt-in via ENABLE_AI_TEAM.
# In P4 this owns the store + read-only cockpit; the sweep/reactive loops are
# added in a later phase.
_ai_team_enabled = os.getenv('ENABLE_AI_TEAM', 'false').lower() in ('true', '1')
if _ai_team_enabled:
    from openhands.app_server.team.service import get_team_service

    _team_service = get_team_service()

    @contextlib.asynccontextmanager
    async def _team_lifespan(app):
        async with _team_service:
            yield

    lifespans.append(_team_lifespan)


app = FastAPI(
    title='OpenHands',
    description='OpenHands: Code Less, Make More',
    version=get_version(),
    lifespan=combine_lifespans(*lifespans),
    routes=[Mount(path='/mcp', app=mcp_app)],
)


@app.exception_handler(AuthenticationError)
async def authentication_error_handler(request: Request, exc: AuthenticationError):
    return JSONResponse(
        status_code=401,
        content=str(exc),
    )


app.include_router(v1_router.router)
app.include_router(health_router)

if _github_poller_enabled:
    from openhands.app_server.github_poller.router import (
        router as github_poller_router,
    )

    # Mounted under /api/v1 so it shares host/port with the rest of the app.
    app.include_router(github_poller_router, prefix='/api/v1')

if _ai_team_enabled:
    from openhands.app_server.team.router import router as team_router

    app.include_router(team_router, prefix='/api/v1')

# Middleware and static file setup (merged from listen.py)
if os.getenv('SERVE_FRONTEND', 'true').lower() == 'true':
    if os.path.isdir('./frontend/build'):
        app.mount(
            '/', SPAStaticFiles(directory='./frontend/build', html=True), name='dist'
        )

app.add_middleware(LocalhostCORSMiddleware)
app.add_middleware(CacheControlMiddleware)
app.add_middleware(
    RateLimitMiddleware,
    rate_limiter=InMemoryRateLimiter(requests=10, seconds=1),
)
