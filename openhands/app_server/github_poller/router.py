"""Router for the GitHub poller: repo-CRUD API + a small management UI.

Mounted under the V1 router at ``/api/v1/github-poller`` so it shares the same
host/port and lifecycle as the rest of the app-server. Only mounted when the
poller is enabled (see ``app.py``).
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from openhands.app_server.github_poller.service import get_github_poller_service

router = APIRouter(prefix='/github-poller', tags=['GitHub Poller'])


@router.get('/health')
async def health() -> dict:
    svc = get_github_poller_service()
    return {
        'status': 'ok',
        'watched': len(svc.list_repos(only_enabled=True)),
        'interval_seconds': svc.interval,
        'trigger_label': svc.trigger_label,
        'mention': svc.mention,
        'token_configured': bool(svc.token),
    }


@router.get('/repos')
async def list_repos() -> list[dict]:
    return get_github_poller_service().list_repos()


@router.post('/repos')
async def add_repo(request: Request) -> JSONResponse:
    data = await request.json()
    full_name = (data.get('full_name') or '').strip()
    if '/' not in full_name:
        raise HTTPException(400, "full_name must be 'owner/repo'")
    get_github_poller_service().add_repo(full_name)
    return JSONResponse({'message': 'added', 'full_name': full_name})


@router.patch('/repos/{owner}/{repo}')
async def toggle_repo(owner: str, repo: str, request: Request) -> JSONResponse:
    data = await request.json()
    get_github_poller_service().set_repo_enabled(
        f'{owner}/{repo}', bool(data.get('enabled', True))
    )
    return JSONResponse({'message': 'updated'})


@router.delete('/repos/{owner}/{repo}')
async def delete_repo(owner: str, repo: str) -> JSONResponse:
    get_github_poller_service().delete_repo(f'{owner}/{repo}')
    return JSONResponse({'message': 'deleted'})


@router.get('/monitor-tasks')
async def list_monitor_tasks() -> list[dict]:
    """All persisted monitor tasks (one per repo/stream) with last-run status."""
    return get_github_poller_service().list_monitor_tasks()


@router.post('/sync-agents')
async def sync_agents() -> JSONResponse:
    """Trigger an immediate sync of the watch list from active agents
    (and, if enabled, OpenHands suggested tasks)."""
    svc = get_github_poller_service()
    async with httpx.AsyncClient() as client:
        added = await svc.sync_from_agents(client)
    return JSONResponse({'added': added})


@router.post('/import-suggested')
async def import_suggested() -> JSONResponse:
    """Add the repos OpenHands already knows you work on (its suggested-tasks
    endpoint) to the watch list."""
    svc = get_github_poller_service()
    existing = {x['full_name'] for x in svc.list_repos()}
    added: list[str] = []
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f'{svc.self_url}/api/v1/git/suggested-tasks/search?limit=100', timeout=30.0
        )
        r.raise_for_status()
        for t in r.json().get('items', []):
            repo = t.get('repo')
            if repo and repo not in existing:
                svc.add_repo(repo)
                existing.add(repo)
                added.append(repo)
    return JSONResponse({'added': added})


_INDEX_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>OpenHands GitHub Poller</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;color:#222}
 h1{font-size:20px} .row{display:flex;gap:8px;align-items:center;margin:6px 0}
 input{padding:8px;border:1px solid #ccc;border-radius:6px;flex:1}
 button{padding:8px 12px;border:0;border-radius:6px;background:#111;color:#fff;cursor:pointer}
 button.sec{background:#eee;color:#111} li{list-style:none;display:flex;gap:10px;align-items:center;
 padding:8px;border:1px solid #eee;border-radius:8px;margin:6px 0}
 .grow{flex:1} .muted{color:#888;font-size:12px} .off{opacity:.45}
 code{background:#f4f4f4;padding:1px 5px;border-radius:4px}
</style></head><body>
<h1>OpenHands GitHub Poller</h1>
<p class="muted" id="sub"></p>
<div class="row">
 <input id="repo" placeholder="owner/repo">
 <button onclick="addRepo()">Add repo</button>
 <button class="sec" onclick="importSuggested()">Import from OpenHands</button>
</div>
<ul id="list"></ul>
<script>
const B='./';
async function refreshSub(){
 const h=await (await fetch(B+'health')).json();
 document.getElementById('sub').innerHTML =
  `Polls watched repos every ${h.interval_seconds}s. Triggers: new issues + comments mentioning `+
  `<code>${h.mention}</code>, label-gated by <code>${h.trigger_label}</code>. `+
  (h.token_configured?'':'<b style="color:#c00">GITHUB_POLLER_TOKEN not set.</b>');
}
async function load(){
 const repos = await (await fetch(B+'repos')).json();
 const ul=document.getElementById('list'); ul.innerHTML='';
 for(const x of repos){
   const li=document.createElement('li'); if(!x.enabled) li.className='off';
   li.innerHTML=`<span class="grow"><b>${x.full_name}</b><div class="muted">added ${x.added_at}</div></span>`;
   const t=document.createElement('button'); t.className='sec'; t.textContent=x.enabled?'Disable':'Enable';
   t.onclick=async()=>{await fetch(B+'repos/'+x.full_name,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!x.enabled})});load();};
   const d=document.createElement('button'); d.textContent='Delete';
   d.onclick=async()=>{if(confirm('Remove '+x.full_name+'?')){await fetch(B+'repos/'+x.full_name,{method:'DELETE'});load();}};
   li.appendChild(t); li.appendChild(d); ul.appendChild(li);
 }
 if(!repos.length) ul.innerHTML='<p class="muted">No repos watched yet. Add one or import from OpenHands.</p>';
}
async function addRepo(){
 const v=document.getElementById('repo').value.trim(); if(!v) return;
 const r=await fetch(B+'repos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({full_name:v})});
 if(!r.ok){alert((await r.json()).detail||'error');return;} document.getElementById('repo').value=''; load();
}
async function importSuggested(){
 const d=await (await fetch(B+'import-suggested',{method:'POST'})).json();
 alert('Imported '+(d.added?d.added.length:0)+' repos'); load();
}
refreshSub(); load();
</script></body></html>
"""


@router.get('/ui', response_class=HTMLResponse)
async def ui() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)
