import asyncioi, json, os 
from dotenv import load_dotenv
load_dotenv()
from langchain_mcp_adapters.client import MultiServerMCPClient
from orchestrator.supervisor import prepare, execute_create

async def main():
    mcp = MultiServerMCPClient({
        "aci_kb": {"command": os.environ["ACI_KB_PYTHON"], "args": [os.environ["ACI_KB_SERVER"]], "transport": "stdio"},
        "apic_client": {"command": os.environ["APIC_CLIENT_PYTHON"], "args": [os.environ["APIC_CLIENT_SERVER"]], "transport": "stdio"},
        "skills": {"command": os.environ["SKILLS_PYTHON"], "args": [os.environ["SKILLS_SERVER"]], "transport": "stdio"},
    })
    
    req = "bind the epg 6-BEGUM to port eth1/20 of leaf 102"
    prep = await prepare(req, mcp)
    print("--plan--")
    print("post_path:", prep.get("post_path"))
    print("post_body:")
    print(json.dumps(prep.get("post_body"), indent=2))
    
    print("--executing--")
    final = await execute_create(prep, mcp)
    print("RAW RES:")
    print(json.dumps(final.get("final_result"), indent=2))
    print("--trail--")
    for t in final.get("trail", []):
        print(" ->", t)
        
asyncioi.run_main()