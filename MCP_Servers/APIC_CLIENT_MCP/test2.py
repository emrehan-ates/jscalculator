from dotenv import load_dotenv
load_dotenv()
from apic_client import get_client
import json
res = get_client().get('/api/node/class/fvTenant.json')
print(json.dumps(res, indent=2)[:500])