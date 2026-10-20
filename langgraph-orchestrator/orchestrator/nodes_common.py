"""Shared nodes used by all flows."""
import contextvars
import json, os, re
from itertools import product
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .chat_memory import history_to_messages

_llm = None
def get_llm():
    global _llm
    if _llm is None:
        _llm = ChatOpenAI(
            base_url=os.environ["LITELLM_BASE_URL"],
            api_key=os.environ["LITELLM_API_KEY"],
            model=os.environ["LITELLM_MODEL"],
            temperature=0, ##dene
        )
    return _llm

# Per-request tool registry. A ContextVar (NOT a module global) so concurrent
# Chainlit sessions on different fabrics each see only their own fabric's tools;
# a plain global was silently overwritten by whichever request ran last.
# ContextVar is task-local and is inherited by the LangGraph nodes and by the
# plain async helpers in supervisor.py (which are not graph nodes), so no call
# site has to thread the registry through its signature.
_tools_var: "contextvars.ContextVar[dict]" = contextvars.ContextVar("mcp_tools", default={})

def set_tools(d):
    _tools_var.set(dict(d))
def get_tool(name):
    tools = _tools_var.get()
    if name not in tools:
        raise RuntimeError(f"Tool '{name}' not loaded. Available: {list(tools.keys())}")
    return tools[name]
def get_all_tools(): return dict(_tools_var.get())

def unwrap_mcp_result(result):
    if isinstance(result, list) and len(result) > 0:
        first = result[0]
        if isinstance(first, dict) and "text" in first:
            try: return json.loads(first["text"])
            except json.JSONDecodeError: return first["text"]
        if hasattr(first, "text"):
            try: return json.loads(first.text)
            except json.JSONDecodeError: return first.text
        if isinstance(first, dict): return first
        return first
    if isinstance(result, str):
        try: return json.loads(result)
        except json.JSONDecodeError: return result
    if isinstance(result, dict): return result
    return result

def _extract_balanced(text, open_ch="{", close_ch="}"):
    """Return the first balanced ``open_ch ... close_ch`` span in ``text``,
    respecting JSON string literals and escapes, or ``None`` if there is no
    opening char or the span is unbalanced (e.g. the model truncated it).

    Using brace-balancing instead of a greedy ``{.*}`` regex means a trailing
    ``}`` from a nested object no longer swallows extra characters, and a
    NESTED object is never mistaken for the whole action once the outer braces
    are present.
    """
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _brace_balance(text):
    """Net count of unmatched ``{`` in ``text`` (opens minus closes), ignoring
    braces that appear inside JSON string literals. Positive => that many
    closing ``}`` are missing; negative => extra closes."""
    depth = 0
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    return depth


def extract_json_from_llm(text):
    if not isinstance(text, str):
        text = str(text)
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()

    # 1. Fast path: the whole message is already valid JSON.
    try:
        return json.loads(cleaned)
    except (ValueError, json.JSONDecodeError):
        pass

    # 2. Missing opening brace: the model dropped the leading '{' but the body
    #    still starts with a JSON key (e.g.  "action": "tool_call", ...). Restore
    #    it FIRST, before any nested-object scan, so we don't latch onto a nested
    #    "arguments" object and parse a garbage span. Also repair a missing
    #    TRAILING '}' (the model often drops both), by appending as many closing
    #    braces as are still unbalanced.
    if not cleaned.startswith("{") and re.match(r'^\s*"[^"]+"\s*:', cleaned):
        repaired = "{" + cleaned
        candidates = []
        span = _extract_balanced(repaired)
        if span:
            candidates.append(span)
        missing = _brace_balance(repaired)
        if missing > 0:
            candidates.append(repaired + "}" * missing)
        candidates.append(repaired)
        for cand in candidates:
            try:
                return json.loads(cand)
            except (ValueError, json.JSONDecodeError):
                continue

    # 3. First balanced {...} object anywhere in the text.
    candidate = _extract_balanced(cleaned)
    if candidate:
        try:
            return json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            pass

    # 4. Array fallback.
    m = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (ValueError, json.JSONDecodeError):
            pass

    raise ValueError(f"No JSON found in: {cleaned[:200]}")

INTENT_SYSTEM_PROMPT = """You are an ACI intent classifier with TWO flows: create, agentic.

DETERMINISTIC CREATE (POST) — write operations, approval-gated:
- create_tenant — create a tenant
- create_bd — create a bridge domain inside a tenant
- bind_epg — bind one or more EPGs to one or more ports (static binding)
- bind_vmm_domain — static-bind ALL EPGs of a VMM (VDS) domain to one or more
  ports in one operation. The EPGs and their encap VLANs are discovered from the
  domain automatically; the user only names the domain and the port(s).
- define_port — create a port definition (access policy chain): policy group +
  interface profile + selector + port block, then attach to a switch profile.
  Supports access, port-channel (pc), and vPC.
- copy_epg_bindings — copy ALL EPG static bindings from one source port to ONE
  OR MANY destination ports (10, 15 or more) in one operation (additive merge;
  never deletes). Ports may be given as device serials or as explicit leaf+port.
- copy_port_definition — create a NEW port definition by copying ALL access-
  policy config (type access/pc/vpc, AEP, and every policy: CDP/LLDP/link level/
  storm/STP/LACP) from an EXISTING port, onto a NEW leaf+port the user gives.
  The new name (TEAM_SERIAL) is applied to the policy group, interface profile,
  selector, and all descriptions.
- remove_from_dvs — take one or more servers OUT OF the DVS / VMM ("şu
  sunucuları DVS'ten çıkar", "remove X and Y from the DVS", "baremetal'e al"):
  their data policy groups are moved to the team's baremetal AEP. The AEP is
  found automatically; the user only names the servers.

AGENTIC (investigation, reads, lookups — everything that is not a create):
- Listing / showing / counting / getting objects by type ("list VRFs",
  "show all tenants", "how many BDs", "list all faults").
- The user gives only a NAME with no type ("where is HOST-101").
- ANY why / how / troubleshoot / is-it-healthy / reachability question
  ("why is EPG X unreachable", "why is port eth1/1 down", "is the fabric healthy").
- Asking what is ATTACHED TO or CONTAINED IN a specific NAMED object — its
  static bindings, bound ports, subnets, domains, contracts, or children
  (e.g. "static bound ports of EPG VLAN-109", "subnets of BD X",
  "domains on EPG Y").
- Anything needing multiple correlated lookups or judgement.

DECISION BOUNDARY (core rule):
- CREATE for the write intents above.
- AGENTIC for everything else — reads, lookups, listing/counting, name-only,
  why/how/troubleshoot/health/reachability, and multi-step correlation.

CONVERSATION HISTORY RULES:
- Prior user/assistant turns are context for resolving references such as "it",
    "that EPG", or omitted object names and locations.
- Only the CURRENT user request may authorize a CREATE flow. A prior write
    request, approval, or proposed operation must NEVER turn a bare affirmation
    turn ("yes", "okay, do that", "evet yap") into a write operation.
- Any current request with real content — in any language — may use history to
    fill in missing details. If you classify it as a write, the approval gate
    still applies. When unsure between create and agentic for a request that
    clearly names objects AND a change, prefer create; the gates catch mistakes.

Respond ONLY with a JSON object. Examples:

{"intent": "create_tenant", "flow": "create", "params": {"name": "AI-TEST-7"}, "target_class": "fvTenant"}
{"intent": "bind_epg", "flow": "create", "params": {"epgs": ["MSY_TEST_354"], "mode": "trunk", "bindings": [{"node": "102", "port": "eth1/20"}]}, "target_class": "fvRsPathAtt"}
{"intent": "bind_epg", "flow": "create", "params": {"epgs": ["Raporlama_1", "Raporlama_2"], "mode": "trunk", "bindings": [{"node": "102", "port": "eth1/20"}, {"node": "103", "port": "eth1/5"}]}, "target_class": "fvRsPathAtt"}
{"intent": "bind_epg", "flow": "create", "params": {"epgs": ["VLAN-109"], "mode": "trunk", "bindings": [{"serial": "MSY_CX124553KT"}]}, "target_class": "fvRsPathAtt"}
{"intent": "bind_epg", "flow": "create", "params": {"epgs": ["PROD_WEB"], "mode": "native", "encap": "500", "bindings": [{"node": "101", "port": "eth1/1"}]}, "target_class": "fvRsPathAtt"}
{"intent": "bind_epg", "flow": "create", "params": {"epgs": ["VLAN-109", "VLAN-110"], "mode": "trunk", "bindings": [{"serial": "CX124553KT"}, {"serial": "CX2204300RT"}]}, "target_class": "fvRsPathAtt"}
{"intent": "bind_vmm_domain", "flow": "create", "params": {"vmm_domain": "MSY_VDS", "mode": "trunk", "bindings": [{"node": "101", "port": "eth1/1"}]}, "target_class": "fvRsPathAtt"}
{"intent": "bind_vmm_domain", "flow": "create", "params": {"vmm_domain": "MSY_VDS", "mode": "trunk", "bindings": [{"serial": "MSY_CX124553KT"}]}, "target_class": "fvRsPathAtt"}
{"intent": "define_port", "flow": "create", "params": {"name": "CSY_CZ241906K3", "port_type": "access", "nodes": ["125"], "ports": ["eth1/47"], "aep": "CSY_BAREMETAL"}, "target_class": "infraAccPortGrp"}
{"intent": "define_port", "flow": "create", "params": {"name": "CSY_5R2L5B3", "port_type": "vpc", "nodes": ["125", "126"], "ports": ["eth1/47"], "aep": "ESX-CSY_AEP"}, "target_class": "infraAccBndlGrp"}
{"intent": "define_port", "flow": "create", "params": {"name": "MY_PC", "port_type": "pc", "nodes": ["125"], "ports": ["eth1/47", "eth1/48"], "aep": "SOME_AEP"}, "target_class": "infraAccBndlGrp"}
{"intent": "define_port", "flow": "create", "params": {"name": "DR1_HOST", "port_type": "vpc", "nodes": ["DR1-C6-8", "DR1-C7-8"], "ports": ["eth1/1"], "aep": "DR1_AEP"}, "target_class": "infraAccBndlGrp"}
{"intent": "bind_epg", "flow": "create", "params": {"epgs": ["PROD_DB"], "mode": "trunk", "bindings": [{"node": "DR2-B05-10", "port": "eth1/5"}]}, "target_class": "fvRsPathAtt"}
{"intent":"copy_epg_bindings","flow":"create","params":{"source":{"node":"DR1-C6-8","port":"eth1/12"},"destinations":[{"node":"103","port":"eth1/14"}]},"target_class":"fvRsPathAtt"}
{"intent":"copy_epg_bindings","flow":"create","params":{"source":{"serial":"MSY_CX124553KT"},"destinations":[{"serial":"CX2204300RT"},{"serial":"CZ24160G9N"},{"node":"101","port":"eth1/5"}]},"target_class":"fvRsPathAtt"}
{"intent": "bind_epg", "flow": "create", "params": {"epgs": ["ESX_MGMT_110"], "mode": "trunk", "bindings": [{"serial": "CX124553KT", "scope": "management"}]}, "target_class": "fvRsPathAtt"}
{"intent":"copy_epg_bindings","flow":"create","params":{"source":{"serial":"CX124553KT","scope":"management"},"destinations":[{"serial":"CZ24160G9N","scope":"management"},{"serial":"CX2204300RT","scope":"management"}]},"target_class":"fvRsPathAtt"}
{"intent":"remove_from_dvs","flow":"create","params":{"servers":[{"serial":"CX124553KT"},{"serial":"MSY_CZ24160G9N"}]},"target_class":"infraRsAttEntP"}
{"intent":"copy_port_definition","flow":"create","params":{"name":"CZ241809QS","source":"CZ241908H2","nodes":["125"],"ports":["eth1/47"]},"target_class":"infraAccPortGrp"}
{"intent": "agentic", "flow": "agentic", "params": {}, "target_class": ""}

(Name-only requests like "where is HOST-101", any "why/how/troubleshoot/
is-it-healthy/reachability" question, listing/counting/getting objects by type
("list VRFs", "how many BDs", "list all faults"), AND "the bindings/ports/
subnets/domains/contracts OF a named object" (e.g. "static bound ports of EPG
VLAN-109") are ALL AGENTIC.)

PARAM RULES:
- bind_epg: extract "epgs" (LIST of EPG names — even one is a list), "bindings"
  (LIST of ports), and "mode". EVERY EPG binds to EVERY port. A port entry is ONE of:
    * {"serial": "<device serial OR full interface-profile name>"} — preferred
      when the user names a device. Keep the MSY_/CSY_ prefix if the user wrote
      it; "MSY_CX124553KT" and "CX124553KT" are BOTH valid (substring match
      finds the profile). Non-host objects that do not follow the TEAM_SERIAL
      convention are given by full name in the SAME "serial" field. The program
      finds ALL of the device's ports (every access port, or the PC/vPC bundle).
      Add "scope" to a {"serial"} entry when the user means the host's
      MANAGEMENT ports ("management", "mgmt", "yönetim", "management portları"):
        "scope": "management" -> only the management ports,
        "scope": "all"        -> data AND management ports ("tüm portları",
                                 "data ve management"),
        omit scope            -> the data ports (default).
      Management ports are found automatically in the ESX(I)-Infra-<leaf>
      profiles; NEVER invent management leaf/port values.
    * {"node": "<leaf id>", "port": "eth1/x"} — an explicit single leaf port.
      The "node" value may be a switch hostname (e.g. "DR1-C6-8") instead of a
      numeric ID — the system resolves it automatically.
    * {"node": "<n1>", "node2": "<n2>", "policy_group": "<bundle>"} — explicit vPC.
      "node" and "node2" may be switch hostnames — the system resolves them.
  "mode" is trunk/access/native (default trunk); the program maps trunk->regular,
  access->untagged, native->native. Include "encap" (a VLAN like "500" or
  "vlan-500") ONLY when the user explicitly states a VLAN in the prompt (e.g.
  "with vlan 500", "encap 500"); it then OVERRIDES everything and applies to ALL
  EPGs. If the user gives no VLAN, OMIT encap — it is derived automatically (from
  each EPG name, else its existing static bindings). Do NOT include
  tenant/app_profile. NEVER invent tenant, app_profile, encap, node, or serial.
- bind_vmm_domain: extract "vmm_domain" (the VDS/VMM domain name) and "bindings"
  (LIST of ports, SAME forms as bind_epg: {"serial"}, {"node","port"}, or explicit
  vPC), plus "mode" (trunk/access/native, default trunk). Do NOT extract EPGs or
  encap — every EPG on the domain and its VLAN are discovered automatically.
  NEVER invent the domain name or the port(s). Use this (NOT bind_epg) whenever
  the user wants to bind "all EPGs of a VMM/VDS domain" or "a domain's EPGs".
- define_port: extract "name" (used for BOTH the policy group and the interface
  profile), "port_type" (access | pc | vpc — the user MUST state this), "nodes"
  (LIST of leaf IDs: 1 or more for access, exactly 1 for pc, exactly 2 for vpc), "ports" (LIST
  like ["eth1/47"]), and "aep". Standard policies (CDP, LLDP, link level, storm,
  STP, and LACP for pc/vpc) are applied automatically — include cdp/lldp/
  link_level/storm/stp/lacp ONLY if the user explicitly overrides one. NEVER
  invent the AEP; it must come from the user.
  NOTE: "nodes" entries may be switch hostnames (e.g. "DR1-C6-8") instead of
  numeric IDs — the system resolves them automatically.
- copy_epg_bindings: extract "source" (ONE port) and "destinations" (a LIST —
  even a single destination is a list; include EVERY destination the user
  names, there may be 10, 15 or more). Each port entry is either
  {"serial": "<device serial, keep the MSY_ prefix if the user wrote it>"} OR
  {"node": "<leaf id>", "port": "eth1/x"} OR an explicit vPC
  {"node": "<n1>", "node2": "<n2>", "policy_group": "<bundle>"}. If the user
  gives several ports on the same leaf ("101 eth1/5, eth1/6"), make one entry
  per port. NEVER invent tenant, app_profile, encap, or mode — they are read
  from the source port automatically. NEVER invent a destination. A
  {"serial"} source or destination may carry "scope": "management" | "all"
  exactly as in bind_epg (e.g. "copy the management port EPGs of host A to
  host B's management ports").
- copy_port_definition: extract "name" (the NEW port's TEAM_SERIAL name),
  "source" (the existing port/device serial to copy from — substring match, keep
  any TEAM_ prefix), "nodes" (LIST of leaf IDs for the NEW port; the program
  checks the count matches the source type — 2 for a vPC source, 1 for a pc source,
  1 or more for an access source),
  and "ports" (LIST like ["eth1/47"]). Do NOT extract port_type, aep, or any
  policy: they are copied verbatim from the source. NEVER invent the source,
  nodes, or ports.
- remove_from_dvs: extract "servers" (LIST of {"serial": "<serial or full
  interface-profile name>"}, one per server the user names — keep any MSY_/
  CSY_ prefix). Do NOT extract the AEP or team; the baremetal AEP of the
  user's team is looked up automatically. NEVER invent a server.
- agentic: leave "params" empty ({}). The agent extracts what it needs itself.

Class mapping (CREATE only):
- create_tenant -> fvTenant
- create_bd -> fvBD
- bind_epg -> fvRsPathAtt
- bind_vmm_domain -> fvRsPathAtt
- define_port -> infraAccPortGrp (access) or infraAccBndlGrp (pc/vpc)
- copy_epg_bindings -> fvRsPathAtt
- copy_port_definition -> infraAccPortGrp (access) or infraAccBndlGrp (pc/vpc)
- remove_from_dvs -> infraRsAttEntP
- agentic -> (no class)

flow is "create" for create_*/bind_epg/bind_vmm_domain/define_port/copy_epg_bindings/copy_port_definition/remove_from_dvs,
"agentic" for everything else.
"""

# Extra guidance appended to the intent prompt when the active chat profile
# pins a switch-naming convention. Users type the switch reference as loose,
# space-separated segments; without this the classifier may split e.g.
# "7 11 20" into three nodes, or collapse it to "71120" — both of which are
# still handled downstream, but keeping it as one clean node is more reliable.
_CONVENTION_HINTS = {
    "kkb": (
        "NAMING CONTEXT — KKB fabric: switches are named like '711-20' (full "
        "form A1-711-20). Users usually type a switch as space-separated numbers, "
        "e.g. '7 11 20' means the SINGLE switch 711-20, and '05 01 20' means "
        "501-20. Capture the ENTIRE switch reference as ONE string in a 'nodes' "
        "entry (e.g. nodes: [\"7 11 20\"]). NEVER split '7 11 20' into three "
        "nodes and do NOT merge the digits into '71120'. List multiple 'nodes' "
        "ONLY when the user clearly names multiple distinct switches (e.g. a vPC "
        "pair, separated by 'and'/'ve'/','/'&')."
    ),
    "evm": (
        "NAMING CONTEXT — EVM fabric: switches are named like 'DR2-C05-08'. "
        "Users may type them with spaces, e.g. 'DR2 C6 8' means the SINGLE switch "
        "DR2-C05-08. Capture the ENTIRE switch reference as ONE string in a "
        "'nodes' entry (e.g. nodes: [\"DR2 C6 8\"]). NEVER split one switch "
        "reference into multiple nodes. List multiple 'nodes' ONLY when the user "
        "clearly names multiple distinct switches (e.g. a vPC pair)."
    ),
}

# Words that make up a BARE affirmation / vague continuation turn: "yes",
# "okay, do that", "evet yap", "tamam, devam et", "onaylıyorum". A turn built
# ENTIRELY of these tokens carries no write content of its own, so it must
# never inherit write authorization from prior turns. ANY other turn — even
# without an English write verb (e.g. Turkish "EPG'yi eth1/20'ye bağla",
# "tenant'a BD ekle", or a bare parameter answer "102 eth1/20") — is allowed
# to proceed to the create flow; the approval gate still protects the POST.
# NOTE: this is deliberately NOT an English write-verb whitelist. That variant
# silently demoted every legitimate non-English create to the read-only
# agentic flow as soon as chat history became non-empty (only the first
# create of a conversation worked).
_AFFIRMATION_TOKENS = frozenset({
    # English
    "yes", "yeah", "yep", "yup", "ok", "okay", "okey", "sure", "fine",
    "please", "pls", "go", "ahead", "do", "it", "that", "this", "them",
    "then", "apply", "applies", "applied", "continue", "proceed", "confirm",
    "confirmed", "approve", "approved", "send", "execute", "run",
    # Turkish
    "evet", "tamam", "tamamdir", "tamamdır", "olur", "peki", "okey",
    "onayla", "onayla", "onayliyorum", "onaylıyorum", "onayliyoruz",
    "uygula", "uygulayin", "uygulayın", "yap", "yapsana", "yapalim", "yapalım",
    "devam", "et", "edin", "ederim", "bunu", "onu", "bunlari", "bunları",
    "onlari", "onları", "lutfen", "lütfen", "gonder", "gönder", "baslat",
    "başlat", "hadi", "kesin", "istedigim", "istediğim",
})


def _current_request_authorizes_write(user_input: str) -> bool:
    """False only for a BARE affirmation / vague continuation ("yes",
    "okay, do that", "evet yap") — those may never start a write by
    themselves. Any turn with real content authorizes the create flow, in any
    language; the approval gate remains the actual write protection."""
    text = (user_input or "").strip().lower()
    if not text:
        return False
    # Unicode-aware tokens: handles Turkish letters (ç, ğ, ı, İ, ö, ş, ü)
    # and any punctuation the user typed (apostrophes, commas, slashes).
    tokens = re.findall(r"[^\W_]+", text, re.UNICODE)
    if not tokens:
        return False
    return not all(t in _AFFIRMATION_TOKENS for t in tokens)


def _intent_messages(state, system_prompt=None):
    messages = [SystemMessage(content=system_prompt or INTENT_SYSTEM_PROMPT)]
    hint = _CONVENTION_HINTS.get(state.get("convention"))
    if hint:
        messages.append(SystemMessage(content=hint))
    history = history_to_messages(state.get("chat_history"))
    if history:
        messages.append(SystemMessage(content=(
            "The following messages are prior conversation context. The final "
            "HumanMessage after them is the CURRENT request."
        )))
        messages.extend(history)
    messages.append(HumanMessage(content=state["user_input"]))
    return messages


async def recognize_intent(state):
    llm = get_llm()
    messages = _intent_messages(state)
    response = await llm.ainvoke(messages)
    try:
        parsed = extract_json_from_llm(response.content)
    except (ValueError, json.JSONDecodeError) as e:
        return {
            "intent": "agentic", "flow": "agentic", "params": {}, "target_class": "",
            "errors": [f"Intent parse failed, defaulting to agentic: {e}"],
            "trail": ["recognize_intent: parse failed -> agentic"],
        }
    if (
        parsed.get("flow") == "create"
        and state.get("chat_history")
        and not _current_request_authorizes_write(state.get("user_input", ""))
    ):
        return {
            "intent": "agentic", "flow": "agentic", "params": {}, "target_class": "",
            "trail": ["recognize_intent: bare affirmation turn cannot inherit "
                      "write from history -> agentic"],
        }
    return {
        "intent": parsed.get("intent", "agentic"),
        "flow": parsed.get("flow", "agentic"),
        "params": parsed.get("params", {}),
        "target_class": parsed.get("target_class", ""),
        "trail": [f"recognize_intent: {parsed.get('intent')} (flow={parsed.get('flow')})"],
    }

async def login(state):
    tool = get_tool("apic_login")
    raw = await tool.ainvoke({})
    result = unwrap_mcp_result(raw)
    if not isinstance(result, dict):
        return {"errors": [f"Login bad format: {type(result).__name__}"], "trail": ["login: bad format"]}
    if "error" in result:
        return {"errors": [f"Login failed: {result.get('error')}"], "trail": ["login: FAILED"]}
    return {"trail": [f"login: {result.get('status', 'ok')}"]}

# ---------------------------------------------------------------------------
# fabricNode name resolution
# ---------------------------------------------------------------------------

def _toggle_zero_padding(text: str) -> list[str]:
    """Generate alternate forms of a switch-name fragment by toggling
    zero-padding on 1-2 digit numeric segments.

    Examples:
        "DR1-C6-8"   -> ["DR1-C6-8", "DR1-C06-8", "DR1-C6-08", "DR1-C06-08"]
        "DR1-C06-08" -> ["DR1-C06-08", "DR1-C6-08", "DR1-C06-8", "DR1-C6-8"]
        "A1-512-20"  -> ["A1-512-20", "A01-512-20"]  (512 is 3+ digits, unchanged)

    Returns up to 4 variants (capped to avoid combinatorial explosion).
    """
    parts = text.split("-")
    # For each segment, build a list of variant strings
    segment_variants = []
    for part in parts:
        # Match a segment that starts with letters and ends with 1-2 digits
        # e.g. "C6" -> ("C", "6"), "B05" -> ("B", "05"), "DR1" -> ("DR", "1")
        m = re.match(r"^([A-Za-z]+)(\d{1,2})$", part)
        if m:
            prefix, num = m.group(1), m.group(2)
            variants = set()
            variants.add(prefix + num)  # original
            n = int(num)
            variants.add(prefix + str(n))          # no padding
            variants.add(prefix + f"{n:02d}")       # 2-digit padding
            segment_variants.append(sorted(variants))
        elif re.match(r"^\d{1,2}$", part):
            # Pure 1-2 digit number segment (e.g. "8", "10")
            n = int(part)
            variants = sorted({str(n), f"{n:02d}"})
            segment_variants.append(variants)
        else:
            # 3+ digit numbers or non-numeric segments: no variants
            segment_variants.append([part])

    # Build cross-product, but cap at 4 variants
    combos = []
    for combo in product(*segment_variants):
        combos.append("-".join(combo))
        if len(combos) >= 4:
            break
    return combos


def _is_valid_switch_name(name: str) -> bool:
    """Heuristic: does *name* look like it could be a switch name fragment?

    A switch name fragment should contain at least one hyphen and at least one
    alphanumeric segment (e.g. "DR1-C6-8" -> True, "hello" -> False, "101" -> False).
    Used only for the warning message — the APIC query is the source of truth.
    """
    if not name or not isinstance(name, str):
        return False
    # Pure digits = numeric node ID, not a name
    if re.match(r"^\d{1,4}$", name.strip()):
        return False
    # Must contain at least one hyphen and at least one letter or digit
    if "-" not in name:
        return False
    return bool(re.search(r"[A-Za-z0-9]", name))


# ---------------------------------------------------------------------------
# Site naming conventions (chat-profile driven)
# ---------------------------------------------------------------------------

def detect_switch_convention(profile) -> str | None:
    """Map a chat-profile / fabric name to its switch-naming convention.

    Profiles whose name starts with 'EVM' use the <LEFT>-C<nn>-<nn> convention;
    those starting with 'KKB' use the A1-<nnn>-<nn> convention (the left segment
    is always A1). Anything else -> None (no site-specific normalization)."""
    if not profile:
        return None
    p = str(profile).strip().upper()
    if p.startswith("EVM"):
        return "evm"
    if p.startswith("KKB"):
        return "kkb"
    return None


def _frag_tokens(raw) -> list[str]:
    """Split a user-typed switch fragment into alphanumeric tokens, ignoring
    whatever separators the user did (or did not) type: 'DR2 C6 8',
    'DR2-C05-08' and 'DR2/C6/8' all -> ['DR2', 'C6', '8']."""
    return [t for t in re.split(r"[^A-Za-z0-9]+", str(raw).strip()) if t]


def _pad2(num: str) -> str:
    return f"{int(num):02d}"


def _normalize_evm(raw: str) -> list[str]:
    """Canonicalize an EVM switch fragment to <LEFT>-C<nn>-<nn>.

    The middle numeric part and the right part are zero-padded to 2 digits; the
    left segment is kept as the user typed it. Returns candidate fragments
    (canonical first); a few padding variants are included so a single OR'd
    wcard query tolerates non-standard fabric naming. Returns [] when the input
    does not match the expected 3-part shape (caller falls back)."""
    tokens = _frag_tokens(raw)
    if len(tokens) != 3:
        return []
    left, mid, right = tokens
    m = re.match(r"^([A-Za-z]+)(\d+)$", mid)
    if not m or not right.isdigit():
        return []
    letters, mnum = m.group(1), m.group(2)
    mids = {letters + _pad2(mnum), letters + str(int(mnum))}
    rights = {_pad2(right), str(int(right))}
    canonical = f"{left}-{letters + _pad2(mnum)}-{_pad2(right)}"
    variants = [canonical]
    for mm in mids:
        for rr in rights:
            v = f"{left}-{mm}-{rr}"
            if v not in variants:
                variants.append(v)
    return variants


def _normalize_kkb(raw: str) -> list[str]:
    """Canonicalize a KKB switch fragment. The left segment is always 'A1', so
    the user only supplies the middle (3-digit) and right (2-digit) parts.

    The middle may arrive already combined ('511 20' -> 511) or split
    ('7 11 20' -> 711, '05 01 20' -> 501: the first token is stripped of leading
    zeros, the next is zero-padded to 2 digits). It may also arrive as a single
    dashless blob ('31121' -> middle 311 + right 21, '3118' -> 311 + right 08):
    the middle is always the first 3 digits, the right is whatever follows. The
    right part is zero-padded to 2 digits when single-digit. Returns candidate
    fragments (canonical first) for a single OR'd query, or [] when the input
    isn't the expected shape."""
    tokens = [t for t in _frag_tokens(raw) if t.isdigit()]
    if not tokens:
        return []
    if len(tokens) == 1:
        # Single dashless blob: middle is the first 3 digits, right is the rest.
        combined = tokens[0]
        if len(combined) < 4:
            return []  # too short to be a middle(3) + right(1+) pair
        middle, right = combined[:3], combined[3:]
    else:
        right = tokens[-1]
        mid_tokens = tokens[:-1]
        if len(mid_tokens) == 1:
            middle = mid_tokens[0]
        elif len(mid_tokens) == 2:
            middle = str(int(mid_tokens[0])) + _pad2(mid_tokens[1])
        else:
            middle = str(int(mid_tokens[0])) + "".join(_pad2(t) for t in mid_tokens[1:])
    rights = {_pad2(right), str(int(right))}
    canonical = f"A1-{middle}-{_pad2(right)}"
    variants = [canonical]
    for prefix in (f"A1-{middle}", middle):
        for rr in rights:
            v = f"{prefix}-{rr}"
            if v not in variants:
                variants.append(v)
    return variants


def _normalize_switch_fragment(raw: str, convention) -> list[str]:
    """Return the search fragments for *raw* under the given naming *convention*.
    Falls back to the generic zero-padding toggler when the convention does not
    apply or the fragment doesn't match its expected shape."""
    if convention == "evm":
        v = _normalize_evm(raw)
        if v:
            return v
    elif convention == "kkb":
        v = _normalize_kkb(raw)
        if v:
            return v
    variants = [raw]
    for alt in _toggle_zero_padding(raw):
        if alt not in variants:
            variants.append(alt)
    return variants


def _or_wcard(fragments: list[str], attr: str = "fabricNode.name") -> str:
    """Build one query-target-filter that OR's a wcard match per fragment, so a
    single GET covers every padding variant instead of one GET per variant."""
    conds = [f'wcard({attr},"{f}")' for f in fragments]
    expr = conds[-1]
    for c in reversed(conds[:-1]):
        expr = f"or({c},{expr})"
    return expr


async def resolve_node_name(name_input: str, convention: str | None = None) -> dict:
    """Resolve a switch name fragment to a fabricNode ID.

    If *name_input* is pure digits (1-4 digits), it is returned as-is
    (backward compatible with existing numeric node IDs like "101").

    Otherwise the fragment is normalized to the site convention (*convention*
    is "evm", "kkb", or None) — fixing wrong/missing zero-padding and combining
    space-separated tokens — then ALL candidate padding variants are queried in
    ONE fabricNode wcard query OR'd together (a single GET instead of one GET
    per variant).

    Returns on success:
        {"node_id": "119", "node_name": "ESY-B-DR2-B05-10-119",
         "pod": "1", "error": None}

    Returns on failure:
        {"node_id": None, "node_name": None, "pod": None, "error": "..."}
    """
    # --- Backward compatibility: pure numeric IDs pass through ---
    # Exception: under the KKB convention a 4+ digit numeric is a combined
    # switch reference (e.g. "31121" -> A1-311-21, "3118" -> A1-311-08), so it
    # must be normalized rather than treated as a node ID.
    stripped = str(name_input).strip()
    if re.match(r"^\d{1,4}$", stripped) and not (convention == "kkb" and len(stripped) >= 4):
        return {
            "node_id": str(int(stripped)),
            "node_name": None,
            "pod": None,
            "error": None,
        }

    name_input = str(name_input).strip()

    async def _query_fabric_node(fragments: list[str]) -> list[dict]:
        """Query fabricNode with a single OR'd wcard filter over *fragments*,
        returning the list of matching attribute dicts (deduped by the API)."""
        tool = get_tool("apic_get")
        raw = await tool.ainvoke({
            "path": "/api/node/class/fabricNode.json",
            "params": {"query-target-filter": _or_wcard(fragments)},
        })
        result = unwrap_mcp_result(raw)
        if not isinstance(result, dict) or "imdata" not in result:
            return []
        matches = []
        for item in result.get("imdata", []):
            if isinstance(item, dict) and "fabricNode" in item:
                attrs = item["fabricNode"].get("attributes", {})
                if attrs.get("name"):
                    matches.append(attrs)
        return matches

    # --- Normalize to the site convention and de-dup the candidate fragments ---
    fragments, seen = [], set()
    for f in _normalize_switch_fragment(name_input, convention):
        if f and f not in seen:
            seen.add(f)
            fragments.append(f)
    canonical = fragments[0] if fragments else name_input

    try:
        await get_tool("apic_login").ainvoke({})
    except Exception:
        pass  # Login may already be active; non-fatal

    # --- One OR'd query covers every padding/combination variant ---
    matches = await _query_fabric_node(fragments)

    # --- No match found ---
    if not matches:
        hint = ""
        if not _is_valid_switch_name(canonical):
            hint = (" The input does not look like a switch name. "
                    "Provide a valid switch name (e.g. DR1-C6-8, DR2-B05-10) "
                    "or a numeric node ID (e.g. 119).")
        else:
            hint = (" Please check the spelling or provide a more specific "
                    "name fragment, or use a numeric node ID (e.g. 119).")
        return {
            "node_id": None,
            "node_name": None,
            "pod": None,
            "error": f"Could not find a switch matching '{name_input}'.{hint}",
        }

    # --- Multiple matches: disambiguate ---
    if len(matches) > 1:
        # Try to find an exact match among the results, preferring the canonical
        # (normalized) fragment over the raw user input.
        exact = [m for m in matches if m["name"].endswith("-" + canonical.split("-")[-1])
                 and canonical in m["name"]]
        if len(exact) == 1:
            matches = exact
        else:
            names = [m["name"] for m in matches[:10]]
            return {
                "node_id": None,
                "node_name": None,
                "pod": None,
                "error": (
                    f"Multiple switches match '{name_input}': "
                    + ", ".join(names)
                    + (f" ... and {len(matches) - 10} more" if len(matches) > 10 else "")
                    + ". Please provide a more specific name fragment."
                ),
            }

    # --- Single match: extract node ID ---
    attrs = matches[0]
    node_name = attrs["name"]
    node_id = attrs.get("id", node_name.rsplit("-", 1)[-1])

    # Extract pod ID from DN: topology/pod-{id}/node-{id}
    pod = None
    dn = attrs.get("dn", "")
    dn_match = re.search(r"topology/pod-(\d+)", dn)
    if dn_match:
        pod = dn_match.group(1)

    return {
        "node_id": node_id,
        "node_name": node_name,
        "pod": pod,
        "error": None,
    }


async def interpret_operation(user_input: str, summary: str) -> str:
    llm = get_llm()
    resp = await llm.ainvoke([
        SystemMessage(content=(
            "You are an ACI assistant. In ONE short paragraph (2-3 sentences max), "
            "interpret the result of this operation for a network engineer. "
            "Be concise and factual. No bullet points, no headers, no lists."
        )),
        HumanMessage(content=f"User request: {user_input}\n\nOperation result: {summary}"),
    ])
    return resp.content.strip()

