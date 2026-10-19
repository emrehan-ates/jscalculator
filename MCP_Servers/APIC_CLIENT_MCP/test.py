from dotenv import load_dotenv
load_dotenv()
from apic_client import get_client
res = get_client().login(force=True)
print(res)