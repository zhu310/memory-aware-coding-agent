"""Client for the restricted browser worker; screenshots become durable assets."""
import base64,os,uuid
import httpx
from final_version_app.domain.services import get_service_manager
from final_version_app.storage.assets import get_asset_store

def check_browser(port,path='/',actions=None):
    services=get_service_manager().status(port)
    if not services or not services[0]['running']:raise ValueError('Start a managed service before checking it in the browser')
    endpoint=os.getenv('AGENT_BROWSER_URL','');token=os.getenv('AGENT_BROWSER_TOKEN','')
    if not endpoint or not token:raise ValueError('Browser verification worker is not configured')
    with httpx.Client(trust_env=False,timeout=100) as client:
        response=client.post(endpoint.rstrip('/')+'/check',headers={'Authorization':'Bearer '+token},json={'port':port,'path':path,'actions':actions or []})
    if response.status_code>=400:
        try:detail=response.json().get('detail','Browser verification failed')
        except ValueError:detail=f'Browser worker HTTP {response.status_code}'
        raise ValueError(detail)
    result=response.json();data=base64.b64decode(result.pop('screenshot_base64'),validate=True)
    asset=get_asset_store().create('browser-'+uuid.uuid4().hex[:12]+'.png',data,{'source':'browser_check','port':port,'path':path})
    result['screenshot_file_id']=asset['id'];result['screenshot_url']=asset['download_url'];result['preview_url']=services[0]['preview_url']
    return result
