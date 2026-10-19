import asyncio
from server import list_skills, read_skill

# Sync test via the functions (FastMCP tools are sync-callable)
print('=== list_skills ===')
print(list_skills())
print()
print('=== read_skill: aci_object_summary ===')
result = read_skill('aci_object_summary')
print(f"Title: {result.get('title')}")
print(f"Size: {result.get('size_bytes')} bytes")
print(f"Content preview:")
print(result.get('content', '')[:300])
