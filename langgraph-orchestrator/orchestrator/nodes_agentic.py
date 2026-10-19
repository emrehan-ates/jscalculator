"""
Agentic flow nodes — custom ReAct-style loop with loop guards.

This is now the handler for EVERYTHING except the four deterministic CREATE
operations (create_tenant, create_bd, bind_epg, define_port). All reads,
lookups, inventory questions, and troubleshooting run here, driven by the
markdown skills exposed through the `skills` MCP server.

Improvements over the basic loop:
  - dedup: the exact same (tool, arguments) is never executed twice; the agent
    is told it already tried it and must try something different or finalize.
  - fail-fast: after N consecutive empty/failed tool results, stop querying
    and make ONE final LLM call that interprets everything gathered so far
    (also used when the iteration cap is hit). If every query came back empty
    it says honestly that the object was not found.
  - explicit 0-result feedback so the agent changes class/filter, not repeats.
  - structure-aware tool-output formatting: APIC responses always lead with an
    authoritative totalCount/object-count header and are serialized compactly,
    so the model gets the full data and never miscounts from a trimmed body.
  - write tools (POST/DELETE) are hard-blocked: writes must go through the
    deterministic create flow, never the agent.
"""
import json
import logging
import re
from datetime import datetime, timezone
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from .chat_memory import history_to_messages
from .nodes_common import (
    get_llm, get_all_tools, unwrap_mcp_result, extract_json_from_llm, login
)

# Per-feature loggers (dotted namespace, matching aci_kb.retrieval in
# agentic_aci_kb.py). aci_kb.routing = soft skill-selection decisions;
# aci_kb.plan = the one-shot investigation plan text (for later eval).
_routing_logger = logging.getLogger("aci_kb.routing")
_plan_logger = logging.getLogger("aci_kb.plan")

MAX_AGENT_ITERATIONS = 30
FAIL_FAST_EMPTY = 3            # stop after this many empty/failed results in a row
# Once the turn already has real data, a few empty enrichment lookups are
# normal (e.g. walking several interface profiles); allow a longer streak.
FAIL_FAST_EMPTY_WITH_DATA = 5
# How many times per turn to nudge the model to re-emit when it returns
# JSON-looking output that fails to parse (missing leading '{', unescaped inner
# quotes, truncation). Beyond this we stop retrying and use the text as-is.
MAX_MALFORMED_RETRIES = 2
# Byte budget for a single tool result placed into the agent context. This is a
# safety ceiling, NOT a blind character cut: results are serialized compactly and
# only trimmed by WHOLE objects when they exceed this, with the authoritative
# totalCount always preserved (see _format_tool_result).
TOOL_RESULT_LIMIT = 24000
# totalCount at or above this → LARGE RESULT guidance in the tool result text.
LARGE_RESULT_THRESHOLD = 200
# Recommended sample size when the agent must re-query with an explicit page-size.
SAMPLE_PAGE_SIZE = 25
WRITE_TOOLS = {"apic_post", "apic_delete"}
OBSERVATION_LIMIT = 12
_LARGE_RESULT_GUIDANCE = (
    "LARGE RESULT: do NOT fetch remaining pages to dump all objects. "
    "Answer with totalCount plus patterns from the sample shown. "
    f"If more detail is needed, narrow the filter (severity/time/node/port) "
    f"and re-query with page-size≤{SAMPLE_PAGE_SIZE} and page=0 only."
)

AGENT_SYSTEM_PROMPT = """You are an ACI (Cisco Application Centric Infrastructure)
expert agent. You help network engineers investigate, troubleshoot, and
understand their ACI fabric. Every read, lookup, inventory question, and
troubleshooting request comes to you — only the four deterministic CREATE
operations are handled elsewhere.

You act in a loop. Each step, respond with ONLY a JSON object — no markdown
fences, no prose around it — in one of these two forms:

{"action": "tool_call", "tool": "<tool_name>", "arguments": {<json args>}}
{"action": "final_answer", "answer": "<your complete answer>"}

HOW TO WORK
- CRITICAL: Check the AVAILABLE SKILLS list below (names + one-line summaries
  only, no full text yet). If any summary matches your task, call
  read_skill(name) to load its FULL content BEFORE making any apic_get call,
  then FOLLOW ITS STEPS EXACTLY — do not improvise APIC queries. A keyword
  hint may be shown next to the list; treat it as a non-binding suggestion
  only — use your own judgement about which skill(s) truly fit, especially
  for non-English input where the hint may be wrong or missing.
- If nothing in the list clearly fits, read the "generic_investigation" skill
  for a generic read-only investigation procedure instead of guessing.
- "What is X / X nedir / X'i özetle" → aci_object_summary. Relations between
  port, policy group, AEP, phys/VMM domain and VLAN pool → aci_access_policy_chain.
  EPG static bindings / EPGs on a port or server → aci_epg_bindings. Server
  serials, MSY_/CSY_/SG_ names and other site naming → aci_naming_conventions.
- Contracts are out of scope (vzAny permitAny): never query vz* classes,
  fvRsCons or fvRsProv.
- NEVER fabricate DNs, node IDs, or filter values. Resolve them from actual
  APIC data first. For example, if asked about faults for a named policy group,
  you MUST resolve the name to leaf node+port values (see the
  fault_investigation skill) before querying faultInst or faultRecord.
- For read operations (GET, list, find) proceed freely.
- COUNTING/INVENTORY ("how many X", "how much", "list all"): every APIC class
  GET response ALREADY includes an authoritative "totalCount" field (you will
  see it highlighted at the top of the tool result as "APIC response:
  totalCount=N, objects_returned=M."). For a plain count, call the class
  endpoint with the real scoping filter you need (if any) and read totalCount
  from that header. For high-cardinality classes (faultRecord, eventRecord,
  aaaModLR, fvCEp, unscoped faultInst) pass page-size=1 when you only need the
  count, or a small page-size (about 25) with order-by when a sample helps.
  Do NOT add query-target=count and do NOT write eq(<Class>.<prop>,"*") as a
  "wildcard" — "*" is not a valid literal to match with eq(); that filter
  matches nothing. Use wcard() only for genuine substring matches, or no
  filter at all for a fabric-wide count.
- FEW ESSENTIAL CALLS: prefer a short sample over dumping large result sets.
  For fabric/global health overviews, start with active faultInst (severity
  order or critical/major filter) and/or faultSummary — never unscoped
  multi-page faultRecord crawls. When a tool result says LARGE RESULT or
  totalCount ≫ objects shown, finalize from totalCount + the sample, or one
  narrower filtered page=0 query — do not advance page=1,2,3… to reconstruct
  the full set.
- NEVER perform writes. apic_post / apic_delete are blocked here. If the user
  asks to change something, give a final_answer explaining that writes go
  through the approval/create flow (e.g. create tenant/BD, bind EPG, define
  port) and describe what would be changed.
- If you don't know an exact class name or DN, use aci_lookup_class to look it
  up rather than guessing. The most relevant classes for this request may
  already be listed in an "ACI object-model classes likely relevant..." hint
  above — prefer those exact class names.

FINDING A NAMED OBJECT (important)
- A name or label (e.g. "HOST-101", "MSY_SWProf") is NOT necessarily an
  endpoint. Only treat something as an endpoint (fvCEp) when it is a MAC address.
- When asked to find / locate / "where is" an object by name and you don't know
  its type, read the "aci_naming_conventions" skill: it tells which class a
  name pattern belongs to (serial / MSY_<SERIAL> -> interface profile or PC/vPC
  policy group, never an EPG). Then query the likely classes by name AND
  description, e.g. or(eq(<class>.name,"X"),wcard(<class>.descr,"X")).
- Try a DIFFERENT class or filter on each step.

SWITCH NAME RESOLUTION (important)
- Users often refer to switches by their fabricNode name (e.g. "DR1-C6-8",
  "DR2-B05-10") instead of numeric node IDs. When you need a numeric node ID
  for a query and the user gave a name-like string (contains letters and
  hyphens, not pure digits), resolve it FIRST:
  1. Query fabricNode with wcard: apic_get path="/api/node/class/fabricNode.json"
     params={"query-target-filter": "wcard(fabricNode.name,\"FRAGMENT\")"}
  2. If padding is uncertain, OR ALL variants into ONE query instead of one GET
     each, e.g. or(wcard(fabricNode.name,"C6"),wcard(fabricNode.name,"C06")).
     EVM sites zero-pad to 2 digits (C6->C05-style); KKB sites use A1-<nnn>-<nn>.
  3. From the matching fabricNode, extract the "id" attribute — that is the
     numeric node ID.
  4. Use the node ID in subsequent queries (e.g. topology/pod-1/node-119).

POLICY GROUP OF A KNOWN PORT (important)
- If prior conversation or this turn already has leaf node + eth port (or a
  fabricPathDn), and the user asks for the policy group / policy grubu /
  interface policy / AEP of that port: read_skill("aci_access_policy_chain")
  and follow **Path PG**. Reuse the known node/port — do NOT re-query fvCEp
  in a loop and do NOT filter infraAccPortGrp/infraAccBndlGrp by eth or MAC
  (those classes are not keyed that way).
- If fabricPathDn already has pathep-[BUNDLE] where BUNDLE is not eth..., that
  BUNDLE name IS the PC/vPC policy group (infraAccBndlGrp) — query it by name.
- If multiple matches, ask the user to be more specific.
- If no matches, tell the user the switch name was not found and suggest
  checking the spelling or using a numeric node ID.

DISCIPLINE (the loop enforces these)
- Never call the same tool with the same arguments twice — it will be blocked.
- If several lookups in a row return nothing, STOP and give a final_answer.
  Do not keep guessing.

FINAL ANSWER RULES
- Interpret EVERY tool result of this turn, not only the last one: report what
  was found (with the real APIC values), then what came back empty or was not
  checked. A partial chain (e.g. port -> policy group found, AEP lookup empty)
  is a useful answer — give it.
- An empty result means "nothing matched that exact filter", not proof that
  the object does not exist; say which lookup was empty.
- Only when EVERY lookup was empty or failed: say plainly that you could not
  find it / do not know, list what you searched, and suggest how to narrow it.
  Never invent values to fill gaps.
- Answer in the user's language (Turkish or English).
- Minimize APIC calls: one read_skill when needed, then follow it; reuse prior
  results; sample with small page-size instead of paging through history logs.
- After a LARGE RESULT sample on the same path+filter, further page-only
  advances are blocked — narrow the filter or give a final_answer.

EXAMPLES

Read a skill that looks relevant from the AVAILABLE SKILLS list:
{"action": "tool_call", "tool": "read_skill", "arguments": {"skill_name": "aci_object_summary"}}

Find a named object across classes (name + description):
{"action": "tool_call", "tool": "apic_get", "arguments": {"path": "/api/node/class/infraAccPortP.json", "params": {"query-target-filter": "or(eq(infraAccPortP.name,\\"HOST-101\\"),wcard(infraAccPortP.descr,\\"HOST-101\\"))"}}}

Final answer:
{"action": "final_answer", "answer": "HOST-101 is an interface profile (infraAccPortP) at uni/infra/accportprof-HOST-101."}
"""

def _format_tools_for_prompt(tools):
    lines = []
    for name, tool in sorted(tools.items()):
        desc = (tool.description or "").strip().split("\n")[0][:150]
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)

def _signature(tool_name, arguments):
    try:
        return tool_name + ":" + json.dumps(arguments, sort_keys=True, ensure_ascii=False)
    except Exception:
        return tool_name + ":" + str(arguments)

def _is_empty_or_error(result):
    if isinstance(result, dict):
        if "error" in result:
            return True
        imdata = result.get("imdata")
        if isinstance(imdata, list) and len(imdata) == 0:
            return True
        if result.get("totalCount") in ("0", 0):
            return True
    if result in (None, "", [], {}):
        return True
    return False

def _format_tool_result(result):
    """Serialize a tool result for the agent WITHOUT corrupting its structure.

    - APIC responses (a dict with an `imdata` list) always lead with an
      authoritative header (totalCount + objects returned) so the model never
      has to infer the size from a possibly-trimmed body.
    - Everything is serialized as COMPACT JSON, so far more real data fits in
      the same budget than the old indented dump did.
    - If the body still exceeds TOOL_RESULT_LIMIT, it is trimmed by WHOLE
      objects (never mid-JSON) and the message states exactly how many objects
      were omitted; the authoritative totalCount stays visible.
    - Large totalCount (or a truncated body) adds explicit guidance: do not
      page-crawl the rest of the set.
    """
    # APIC class/mo response: dict carrying an imdata list
    if isinstance(result, dict) and isinstance(result.get("imdata"), list):
        imdata = result["imdata"]
        raw_total = result.get("totalCount")
        try:
            total_n = int(raw_total) if raw_total is not None else len(imdata)
        except (ValueError, TypeError):
            total_n = len(imdata)
        header = f"APIC response: totalCount={total_n}, objects_returned={len(imdata)}."
        large = total_n >= LARGE_RESULT_THRESHOLD

        full = json.dumps(imdata, ensure_ascii=False, separators=(",", ":"))
        if len(full) <= TOOL_RESULT_LIMIT:
            text = f"{header}\nimdata={full}"
            if large:
                text = f"{header}\n{_LARGE_RESULT_GUIDANCE}\nimdata={full}"
            return text

        # Too large: keep whole objects up to the budget, never cut mid-object.
        kept, size = [], 0
        for obj in imdata:
            piece = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            if kept and size + len(piece) + 1 > TOOL_RESULT_LIMIT:
                break
            kept.append(piece)
            size += len(piece) + 1
        omitted = len(imdata) - len(kept)
        body = "[" + ",".join(kept) + "]"
        return (f"{header}\nNOTE: result too large for context; showing "
                f"{len(kept)} of {len(imdata)} returned objects "
                f"({omitted} omitted here). The totalCount above is authoritative "
                f"— do NOT infer the count from the objects shown.\n"
                f"{_LARGE_RESULT_GUIDANCE}\n"
                f"imdata_partial={body}")

    # Any other dict/list: compact JSON, with a non-corrupting fallback note.
    if isinstance(result, (dict, list)):
        full = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(full) <= TOOL_RESULT_LIMIT:
            return full
        return full[:TOOL_RESULT_LIMIT] + "\n... (truncated; structure may be incomplete)"

    text = str(result)
    if len(text) <= TOOL_RESULT_LIMIT:
        return text
    return text[:TOOL_RESULT_LIMIT] + "\n... (truncated)"

def _extract_query_target(path: str) -> str:
    """Best-effort extraction of APIC class/object hint from request path."""
    if not isinstance(path, str):
        return "unknown"
    m = re.search(r"/class/([^/.]+)\.json", path)
    if m:
        return m.group(1)
    m = re.search(r"/mo/([^/.]+)\.json", path)
    if m:
        return m.group(1)
    # Bare "faultRecord.json" style paths accepted by apic_get.
    m = re.search(r"([^/.]+)\.json", path)
    if m:
        return m.group(1)
    return "unknown"


def _normalize_params(params) -> dict:
    if not isinstance(params, dict):
        return {}
    return {str(k): v for k, v in params.items()}


def _query_family_signature(tool_name: str, arguments: dict) -> str | None:
    """Signature of path+filter with page stripped — used to block page crawls.

    Returns None when the call is not an APIC GET class/mo query.
    """
    if tool_name != "apic_get" or not isinstance(arguments, dict):
        return None
    path = arguments.get("path")
    if not isinstance(path, str) or not path:
        return None
    params = _normalize_params(arguments.get("params"))
    family_params = {
        k: v for k, v in params.items()
        if k.lower() not in {"page", "page-size", "pagesize"}
    }
    try:
        return path + ":" + json.dumps(family_params, sort_keys=True, ensure_ascii=False)
    except Exception:
        return path + ":" + str(family_params)


def _apic_result_total(result) -> int | None:
    if not isinstance(result, dict) or not isinstance(result.get("imdata"), list):
        return None
    raw_total = result.get("totalCount")
    try:
        return int(raw_total) if raw_total is not None else len(result["imdata"])
    except (ValueError, TypeError):
        return len(result["imdata"])


def _capture_observation(tool_name: str, arguments: dict, result) -> dict:
    """Capture compact structured metadata for fallback interpretation."""
    obs = {
        "tool": tool_name,
        "target": _extract_query_target(arguments.get("path", "")) if isinstance(arguments, dict) else "unknown",
        "empty": False,
        "error": None,
        "total": None,
        "returned": None,
    }
    if isinstance(result, dict):
        if "error" in result:
            obs["error"] = str(result.get("error"))
            obs["empty"] = True
            return obs
        imdata = result.get("imdata")
        if isinstance(imdata, list):
            try:
                total = int(result.get("totalCount", len(imdata)))
            except (TypeError, ValueError):
                total = len(imdata)
            obs["total"] = total
            obs["returned"] = len(imdata)
            obs["empty"] = total == 0
            return obs

    obs["empty"] = result in (None, "", [], {})
    return obs

def _max_iter_fallback(observations, called):
    """Build a final answer without extra LLM calls when iteration cap is hit."""
    if observations and isinstance(observations[0], dict):
        total_calls = len(observations)
        errors = [o for o in observations if o.get("error")]
        empties = [o for o in observations if o.get("empty") and not o.get("error")]
        hits = [o for o in observations if not o.get("empty") and not o.get("error")]

        by_target = {}
        for o in observations:
            t = o.get("target") or "unknown"
            by_target[t] = by_target.get(t, 0) + 1
        top_targets = sorted(by_target.items(), key=lambda kv: kv[1], reverse=True)[:4]
        top_targets_text = ", ".join(f"{name} ({cnt})" for name, cnt in top_targets if name != "unknown")

        if hits and not errors:
            verdict = (
                "Sistemde ilgili verileri buldum, ancak bu verilerden tek ve net bir "
                "kök neden/nihai eşleşme çıkarmak için ek daraltma gerekiyor."
            )
        elif hits and errors:
            verdict = (
                "Kısmi veri toplandı, fakat bazı sorgular hata aldığı için analiz "
                "tamamlanamadı."
            )
        elif empties and not hits:
            verdict = (
                "Yaptığım sorguların çoğu boş döndü; bu isim/filtre ile hedef obje "
                "bulunamadı veya farklı bir obje tipinde olabilir."
            )
        else:
            verdict = "Yeterli doğrulanabilir veri toplayamadım."

        summary_lines = [
            f"{MAX_AGENT_ITERATIONS} adım sınırına ulaştım, elimdeki verileri özetliyorum.",
            f"Toplam sorgu: {total_calls} | Veri dönen: {len(hits)} | Boş: {len(empties)} | Hata: {len(errors)}.",
        ]
        if top_targets_text:
            summary_lines.append(f"En çok sorgulanan obje tipleri: {top_targets_text}.")
        summary_lines.append(verdict)
        summary_lines.append(
            "İstersen bir sonraki turda kapsamı daraltarak (obje tipi + isim, tenant, node/EPG) "
            "tek adımda daha net sonuca gidebilirim."
        )
        return "\n\n".join(summary_lines)

    if observations:
        lines = "\n".join(f"- {o}" for o in observations[-6:])
        return (
            f"{MAX_AGENT_ITERATIONS} adım sınırına ulaştım. Elimdeki bulgular:\n"
            f"{lines}\n\n"
            "Bu bulgularla net sonuca varamadım; kapsamı daraltırsan devam edebilirim."
        )

    tried = "; ".join(called) if called else "no successful lookups"
    return (
        "Maksimum agent adımına ulaştım. Bu süre içinde kesin bir sonuca "
        "varamadım. Denenen sorgular: "
        f"{tried}. Lütfen kapsamı daralt (obje tipi/isim) ve tekrar deneyelim."
    )

_SYNTHESIS_INSTRUCTION = (
    "STOP — no more tool calls are allowed ({reason}). Write the final answer "
    "NOW from ALL tool results above in this turn, following the FINAL ANSWER "
    "RULES: first what was found (real APIC values), then which lookups came "
    "back empty or were not checked. If EVERY APIC lookup was empty or failed, "
    "say plainly that you could not find it / do not know and list what you "
    "searched. Do not invent anything. Respond with ONLY "
    '{{"action": "final_answer", "answer": "<answer>"}}.'
)


def _not_found_answer(called, streak):
    return (
        f"I could not find what you asked for. I ran {len(called)} lookups "
        f"and the last {streak} returned nothing. Tried: "
        + "; ".join(called)
        + ". The object may not exist under these names, or it may be a type "
        "I did not query. Tell me the object type and I can query it directly."
    )


async def _synthesize_final_answer(llm, messages, reason, fallback):
    """One last LLM call that turns every tool result gathered this turn into
    the final answer (partial evidence included). `fallback` is a zero-arg
    callable returning a deterministic answer, used only if the LLM call fails
    or returns nothing usable."""
    try:
        response = await llm.ainvoke(
            list(messages)
            + [HumanMessage(content=_SYNTHESIS_INSTRUCTION.format(reason=reason))]
        )
        text = response.content if isinstance(response.content, str) else str(response.content)
        try:
            action = extract_json_from_llm(text)
            if isinstance(action, dict):
                if action.get("action") == "final_answer" and action.get("answer"):
                    text = str(action["answer"])
                elif action.get("action") == "tool_call":
                    text = ""  # model ignored the instruction
        except (ValueError, json.JSONDecodeError):
            pass  # plain prose is an acceptable final answer
        if text.strip():
            return text.strip()
    except Exception as e:
        logging.getLogger(__name__).warning("final synthesis failed: %s", e)
    return fallback()


# Keywords used ONLY as an advisory hint for skill routing (see
# _load_relevant_skills). The key is a substring of the skill filename (after
# stripping "aci_" prefix and ".md" suffix); the value is a list of lowercase
# keywords that — when present in the user input — suggest the skill MIGHT be
# relevant. This is a coarse, English-only heuristic (it under-matches
# non-English input, e.g. Turkish) and NEVER decides for the agent — the agent
# always sees every skill's name + summary and picks for itself via
# read_skill(). The hint is just a nudge shown next to that full list.
_SKILL_KEYWORDS = {
    "fault_investigation": [
        "fault", "alarm", "wrong", "error", "healthy", "health",
        "what is wrong", "troubleshoot", "hata", "arıza", "sağlık",
    ],
    "endpoint_troubleshooting": [
        "endpoint", "mac", "reachab", "learned",
    ],
    "interface_troubleshooting": [
        "interface down", "port down", "down", "flap", "crc", "fcs",
        "link", "physical", "speed", "kapalı",
    ],
    "access_policy_chain": [
        "policy group", "policy grubu", "policygroup", "interface policy",
        "access policy", "policy chain", "aep", "attachable", "vlan pool",
        "physical domain", "phys dom", "vmm domain", "hangi policy",
        "which policy", "ilişki", "relation",
    ],
    "epg_bindings": [
        "static binding", "static bind", "binding", "bound", "bind edil",
        "bağlı epg", "hangi epg", "which epg", "epgs on", "epg'ler",
        "deploy", "fvifconn",
    ],
    "object_summary": [
        "summar", "özet", "nedir", "what is", "properties", "özellik",
        "detail", "detay", "config of", "konfigürasyon", "anlat",
    ],
    "naming_conventions": [
        "msy_", "csy_", "sg_", "serial", "seri no", "naming",
    ],
    "resolve_identifier": [
        "where is", "nerede", "hangi port", "which port", "connected to",
        "bağlı olduğu", "locate", "find server", "sunucu",
    ],
    "inventory_validation": [
        "count", "how many", "inventory", "total", "kaç adet", "kaç tane", "sayısı",
    ],
    "query_syntax": [
        "filter", "wcard", "query",
    ],
    "connectivity_between_endpoints": [
        "path between", "reachability", "can reach", "connectivity", "erişim",
    ],
    "switch_name_resolution": [
        "switch name", "node name", "leaf name", "spine name", "resolve switch",
        "switch id", "node id for", "fabric node",
    ],
    "vmm_visibility": [
        "vcenter", "esxi", "virtual machine", " vm ", "hypervisor",
    ],
    "vpc_health": [
        "vpc", "port-channel", "port channel", "peer link",
    ],
}


# Minimum number of distinct keyword hits for a skill to surface as an
# advisory hint next to the full skill list. Never auto-loaded and never
# binding — the agent still decides via read_skill(). Tunable, not a fabric
# fact.
SKILL_MATCH_THRESHOLD = 1

# Name of the generic meta-skill the agent should read when nothing in the
# AVAILABLE SKILLS list clearly fits. Lives in the skills server as
# generic_investigation.md; its own summary tells the agent when to use it, so
# it is NOT auto-loaded here — the agent reads it like any other skill.
GENERIC_SKILL_NAME = "generic_investigation"


def _score_skills(user_input: str) -> dict[str, int]:
    """Score every known skill by how many of its keywords appear in the input.

    Returns {skill_key: hit_count} for skills with >=1 hit (skill_key is the
    filename stem with the leading "aci_" removed, e.g. "fault_investigation").
    This is the keyword-overlap "confidence" proxy — there is no embedding score
    yet; the count is the signal the threshold is applied to.
    """
    user_lower = user_input.lower()
    scores: dict[str, int] = {}
    for name_key, keywords in _SKILL_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in user_lower)
        if hits:
            scores[name_key] = hits
    return scores


async def _load_relevant_skills(user_input: str):
    """Progressive disclosure skill routing.

    Level 1 (always, cheap): every available skill's name + one-line summary
    is injected into the system prompt, unconditionally. The agent decides for
    itself — using its own language understanding, not a hardcoded English
    keyword list — which skill(s) to actually load, by calling read_skill()
    during the think/act loop. This keeps routing correct for non-English
    input (e.g. Turkish), where a literal English keyword either doesn't
    appear or misleadingly appears inside an unrelated technical term (e.g.
    "kaç tane endpoint var?" contains the word "endpoint" but is actually an
    inventory/count question, not an endpoint-troubleshooting one).

    Level 2 (on demand, not done here): full skill text is fetched ONLY when
    the agent calls read_skill(name) inside think_and_act — nothing is
    pre-loaded in this function anymore.

    The keyword scorer (_score_skills) is kept only as a soft, NON-BINDING
    hint appended next to the metadata list ("keyword overlap suggests X") —
    it never gates or blocks which skill's full content the agent can load,
    and it is never required to be right.

    Returns (block, hinted):
      - block:  text appended to the system prompt (the full skill list +
                optional hint line). "" only if list_skills is unavailable or
                returned nothing.
      - hinted: list of {name, title, score} whose keywords matched — purely
                advisory (used by inject_class_hints/make_plan as a soft
                signal), NOT already-loaded content.
    The chosen hint (and why) is logged via the aci_kb.routing logger.
    """
    all_tools = get_all_tools()
    list_skill_tool = all_tools.get("list_skills")
    if not list_skill_tool:
        return "", []

    try:
        raw = await list_skill_tool.ainvoke({})
        list_result = unwrap_mcp_result(raw)
    except Exception:
        return "", []

    if isinstance(list_result, dict):
        skill_list = list_result.get("skills", [])
    elif isinstance(list_result, list):
        skill_list = list_result
    else:
        return "", []

    if not skill_list:
        return "", []

    # Map each available skill's lookup key -> its real filename stem, for the
    # heuristic hint only (e.g. "aci_fault_investigation" -> "fault_investigation").
    available: dict[str, str] = {}
    meta_lines: list[str] = []
    for skill in skill_list:
        if not isinstance(skill, dict):
            continue
        name = skill.get("name", "")
        available[name.removeprefix("aci_")] = name
        summary = (skill.get("summary") or skill.get("title") or "").strip()
        meta_lines.append(f"- {name}: {summary}" if summary else f"- {name}")

    scores = _score_skills(user_input)
    strong = {k: v for k, v in scores.items()
              if v >= SKILL_MATCH_THRESHOLD and k in available}

    hinted: list[dict] = []
    hint_line = ""
    if strong:
        for name_key, score in sorted(strong.items(), key=lambda kv: kv[1], reverse=True):
            hinted.append({"name": available[name_key], "title": available[name_key], "score": score})
        _routing_logger.info(
            "skill routing | query=%r | heuristic hint (non-binding, threshold=%d) | %s",
            user_input, SKILL_MATCH_THRESHOLD, [(h["name"], h["score"]) for h in hinted],
        )
        hint_names = ", ".join(h["name"] for h in hinted)
        hint_line = (
            f"\n(Non-binding keyword hint: {hint_names} — verify by reading, "
            "do not trust this blindly, especially for non-English questions.)\n"
        )
    else:
        _routing_logger.info(
            "skill routing | query=%r | no heuristic hint (best score=%d < threshold=%d)",
            user_input, max(scores.values()) if scores else 0, SKILL_MATCH_THRESHOLD,
        )

    block = (
        "\n\nAVAILABLE SKILLS (name + one-line summary only — call "
        "read_skill(name) to load the FULL content of any that fit your task "
        f"before making any apic_get/apic_post call; read '{GENERIC_SKILL_NAME}' "
        "if nothing else fits):\n" + "\n".join(meta_lines) + hint_line
    )
    return block, hinted


async def prepare_agent(state):
    tools = get_all_tools()
    tool_list = _format_tools_for_prompt(tools)
    now_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    # Progressive disclosure: inject every skill's name + one-line summary
    # (cheap, always shown) plus a non-binding keyword hint. The agent decides
    # for itself which skill(s) to fully load via read_skill() during the
    # think/act loop — nothing is force-loaded here.
    skill_block, hinted_skills = await _load_relevant_skills(state["user_input"])

    system_full = (
        AGENT_SYSTEM_PROMPT + skill_block
        + "\n\nAvailable tools:\n" + tool_list
        + f"\n\nCURRENT UTC TIME (use this to compute any time window, e.g. "
          f"'last N hours'): {now_utc}"
        + "\n\nPrior user and assistant messages, when present, are conversation "
          "context only. Re-check live fabric facts when the current request "
          "requires current state."
    )
    messages = [SystemMessage(content=system_full)]
    messages.extend(history_to_messages(state.get("chat_history")))
    messages.append(HumanMessage(content=state["user_input"]))
    return {
        "agent_messages": messages,
        "agent_iterations": 0,
        "agent_called": [],
        "agent_observations": [],
        "agent_empty_streak": 0,
        "agent_had_data": False,
        "agent_large_query_families": [],
        "agent_malformed_retries": 0,
        "selected_skills": hinted_skills,
        "trail": [f"prepare_agent: initialized with {len(tools)} tools"
                  + (f"; skill metadata injected"
                     + (f", heuristic hint={[h['name'] for h in hinted_skills]}"
                        if hinted_skills else "")
                     if skill_block else "")],
    }


# Plan-then-execute: one upfront LLM call that turns the user question + class
# hints + tool list (+ selected skill) into a short numbered investigation plan.
# Runs ONCE per user turn (placed between class_hints and the think/act loop,
# outside the loop-back edge), so it adds exactly ONE LLM call per agentic query.
PLAN_SYSTEM_PROMPT = """You are planning a READ-ONLY ACI (Cisco ACI) fabric
investigation before an agent starts querying APIC. Produce a SHORT numbered
plan of 3-5 steps the agent will follow.

For each step state: which ACI object class(es) or tool to use, in what order,
and what result confirms the step succeeded. End by stating the STOP condition
(enough evidence gathered, or the object was not found).

HARD RULES:
- Read-only investigation only. NEVER propose creating, binding, or changing
  anything.
- Do NOT invent thresholds, node IDs, DNs, VLANs, encaps, or any fabric fact —
  the agent resolves those from live APIC data.
- Prefer the EXACT class names listed in the class hints, if any.
- Prefer cheap summary/sample queries first (e.g. faultInst severity sample or
  faultSummary for fabric health). FORBIDDEN as a plan step: fetching all
  faultRecord/eventRecord/aaaModLR pages or any multi-page dump of a large
  class. Use page-size≈25, page=0, read totalCount, then stop or narrow.
- STOP condition must include: totalCount known (when counting/overview) and
  top sample interpreted — not "retrieve every matching object".
- Plain text only. No markdown fences. Maximum ~150 tokens.
"""


async def make_plan(state):
    """Graph node: append ONE SystemMessage holding a short investigation plan.

    Input = last user message + the injected class-hint block + a one-line list
    of available tools + (if the keyword heuristic hinted at one) that skill's
    name, as a non-binding note.
    Exactly one LLM call. Runs once per user turn; the think/act loop never
    returns here. On any error it degrades gracefully (no plan, no crash).
    """
    llm = get_llm()
    user_text = state.get("user_input", "")
    messages = state.get("agent_messages", [])

    # The class-hint block was appended by the class_hints node as a
    # SystemMessage beginning with this exact phrase (see agentic_aci_kb.py).
    class_hint_block = ""
    for m in messages:
        content = getattr(m, "content", None)
        if isinstance(m, SystemMessage) and isinstance(content, str) \
                and content.startswith("ACI object-model classes likely relevant"):
            class_hint_block = content
            break

    tools_oneline = ", ".join(sorted(get_all_tools().keys()))

    hinted = state.get("selected_skills") or []
    skill_line = ""
    if hinted:
        skill_line = "Heuristic keyword hint, unverified — " + ", ".join(
            str(s.get("title") or s.get("name")) for s in hinted)

    plan_input = (
        f"USER QUESTION:\n{user_text}\n\n"
        + (f"{class_hint_block}\n\n" if class_hint_block else "")
        + (f"{skill_line}\n\n" if skill_line else "")
        + f"AVAILABLE TOOLS: {tools_oneline}\n\n"
        + "Write the numbered investigation plan now."
    )

    try:
        response = await llm.ainvoke([
            SystemMessage(content=PLAN_SYSTEM_PROMPT),
            HumanMessage(content=plan_input),
        ])
        plan_text = (response.content or "").strip()
    except Exception as e:
        _plan_logger.warning("plan generation failed for %r: %s", user_text, e)
        return {"trail": ["make_plan: skipped (LLM error)"]}

    if not plan_text:
        return {"trail": ["make_plan: empty plan, skipped"]}

    # One-line log for later eval comparison.
    _plan_logger.info("investigation plan | query=%r | plan=%s",
                      user_text, plan_text.replace("\n", " / "))

    plan_msg = SystemMessage(content=(
        "Investigation plan — follow unless evidence contradicts it:\n" + plan_text
    ))
    return {
        "agent_messages": [plan_msg],
        "trail": ["make_plan: injected 1 plan message"],
    }

async def think_and_act(state):
    llm = get_llm()
    messages = state.get("agent_messages", [])
    iteration = state.get("agent_iterations", 0) + 1
    called = list(state.get("agent_called", []))
    observations = list(state.get("agent_observations", []))
    empty_streak = state.get("agent_empty_streak", 0)
    had_data = state.get("agent_had_data", False)
    large_families = list(state.get("agent_large_query_families", []))

    if iteration >= MAX_AGENT_ITERATIONS:
        answer = await _synthesize_final_answer(
            llm, messages, f"the {MAX_AGENT_ITERATIONS}-step limit was reached",
            lambda: _max_iter_fallback(observations, called))
        return {
            "agent_final_answer": answer,
            "agent_messages": [AIMessage(content=answer)],
            "agent_iterations": iteration,
            "trail": [f"think_and_act: hit max iterations ({MAX_AGENT_ITERATIONS}), synthesized"],
        }

    response = await llm.ainvoke(messages)
    raw_content = response.content

    try:
        action = extract_json_from_llm(raw_content)
    except (ValueError, json.JSONDecodeError):
        # The output is not parseable JSON. Two distinct cases:
        #   (a) the model TRIED to emit an action but the JSON is malformed —
        #       most commonly a missing leading '{', unescaped double quotes
        #       inside a filter value, or a truncated object. Give it a single
        #       corrective nudge to re-emit instead of killing the turn.
        #   (b) the model answered in plain prose — accept that as the final.
        text = raw_content if isinstance(raw_content, str) else str(raw_content)
        looks_like_action = bool(
            re.search(r'"(?:action|tool|arguments|final_answer|answer)"\s*:', text)
        )
        retries = state.get("agent_malformed_retries", 0)
        if looks_like_action and retries < MAX_MALFORMED_RETRIES:
            return {
                "agent_messages": [
                    AIMessage(content=text),
                    HumanMessage(content=(
                        "ERROR: your previous message was not valid JSON, so it "
                        "could not be executed. Common causes: (1) a MISSING opening "
                        "'{' — the object MUST start with '{' and end with '}'; "
                        "(2) UNESCAPED double quotes inside a string value — every "
                        '" inside a value must be written as \\". Re-emit ONLY the '
                        "corrected JSON object, with no prose before or after it. "
                        "Correctly escaped example:\n"
                        '{"action": "tool_call", "tool": "apic_get", "arguments": '
                        '{"path": "/api/node/class/faultRecord.json", "params": '
                        '{"query-target-filter": "and(gt(faultRecord.created,'
                        '\\"2026-07-19T11:15:31+00:00\\"),wcard(faultRecord.affected,'
                        '\\"CSY_CZ242004F8\\"))", "order-by": '
                        '"faultRecord.created|desc"}}}'
                    )),
                ],
                "agent_malformed_retries": retries + 1,
                "agent_iterations": iteration,
                "trail": [f"think_and_act #{iteration}: malformed JSON, asked model to "
                          f"re-emit (retry {retries + 1}/{MAX_MALFORMED_RETRIES})"],
            }
        return {
            "agent_final_answer": text,
            "agent_messages": [AIMessage(content=text)],
            "agent_iterations": iteration,
            "trail": [f"think_and_act #{iteration}: non-JSON response, used as final"],
        }

    action_type = action.get("action")

    # ---- Final answer ----
    if action_type == "final_answer":
        return {
            "agent_final_answer": action.get("answer", ""),
            "agent_messages": [AIMessage(content=raw_content)],
            "agent_iterations": iteration,
            "trail": [f"think_and_act #{iteration}: final_answer"],
        }

    # ---- Tool call ----
    if action_type == "tool_call":
        tool_name = action.get("tool", "")
        arguments = action.get("arguments", {})
        tools = get_all_tools()

        if tool_name not in tools:
            return {
                "agent_messages": [
                    AIMessage(content=raw_content),
                    HumanMessage(content=f"ERROR: Tool '{tool_name}' not available. Available: {list(tools.keys())}"),
                ],
                "agent_iterations": iteration,
                "trail": [f"think_and_act #{iteration}: unknown tool '{tool_name}'"],
            }

        if tool_name in WRITE_TOOLS:
            return {
                "agent_messages": [
                    AIMessage(content=raw_content),
                    HumanMessage(content=(
                        f"BLOCKED: '{tool_name}' is a write operation and needs explicit "
                        f"user approval. Give a final_answer describing what you would do."
                    )),
                ],
                "agent_iterations": iteration,
                "trail": [f"think_and_act #{iteration}: blocked write tool '{tool_name}'"],
            }

        # DEDUP: refuse to run the exact same call twice
        sig = _signature(tool_name, arguments)
        if sig in called:
            return {
                "agent_messages": [
                    AIMessage(content=raw_content),
                    HumanMessage(content=(
                        f"ALREADY TRIED: you already called {tool_name} with these exact "
                        f"arguments and it did not help. Do NOT repeat it. Try a DIFFERENT "
                        f"object class or filter, or give a final_answer."
                    )),
                ],
                "agent_iterations": iteration,
                "trail": [f"think_and_act #{iteration}: duplicate blocked ({tool_name})"],
            }

        # PAGE-CHASE: after a large-result sample on this path+filter family,
        # block further page-only advances (same filter, different page).
        family = _query_family_signature(tool_name, arguments)
        if family and family in large_families:
            return {
                "agent_messages": [
                    AIMessage(content=raw_content),
                    HumanMessage(content=(
                        "BLOCKED: you already sampled this APIC query family "
                        "(same path and filter) and totalCount was large. Do NOT "
                        "advance page to dump more of the same result set. "
                        f"Give a final_answer from totalCount + the sample, or "
                        f"narrow severity/time/node/port and re-query with "
                        f"page-size≤{SAMPLE_PAGE_SIZE} and page=0."
                    )),
                ],
                "agent_iterations": iteration,
                "agent_large_query_families": large_families,
                "trail": [f"think_and_act #{iteration}: page-chase blocked ({tool_name})"],
            }

        # Execute
        try:
            raw_result = await tools[tool_name].ainvoke(arguments)
            result = unwrap_mcp_result(raw_result)
        except Exception as e:
            result = {"error": f"Tool execution failed: {e}"}

        called.append(sig)
        empty = _is_empty_or_error(result)
        new_streak = empty_streak + 1 if empty else 0
        # Only APIC data counts as evidence (a read_skill result is not).
        if not empty and tool_name != "read_skill":
            had_data = True
        observations.append(_capture_observation(tool_name, arguments, result))
        if len(observations) > OBSERVATION_LIMIT:
            observations = observations[-OBSERVATION_LIMIT:]

        # Mark query family after a large hit so later page crawls are blocked.
        total_n = _apic_result_total(result)
        if (
            family
            and total_n is not None
            and total_n >= LARGE_RESULT_THRESHOLD
            and family not in large_families
        ):
            large_families.append(family)

        result_str = _format_tool_result(result)
        note = "(0 results — try a DIFFERENT class or filter) " if empty else ""
        step_messages = [
            AIMessage(content=raw_content),
            HumanMessage(content=f"{note}Tool result for {tool_name}:\n{result_str}"),
        ]

        # FAIL-FAST: too many empty/failed results in a row → stop querying and
        # interpret everything gathered so far in one final LLM call.
        limit = FAIL_FAST_EMPTY_WITH_DATA if had_data else FAIL_FAST_EMPTY
        if new_streak >= limit:
            answer = await _synthesize_final_answer(
                llm, messages + step_messages,
                f"the last {new_streak} lookups returned nothing",
                lambda: _not_found_answer(called, new_streak))
            return {
                "agent_final_answer": answer,
                "agent_messages": step_messages + [AIMessage(content=answer)],
                "agent_called": called,
                "agent_observations": observations,
                "agent_empty_streak": new_streak,
                "agent_had_data": had_data,
                "agent_large_query_families": large_families,
                "agent_iterations": iteration,
                "trail": [f"think_and_act #{iteration}: fail-fast after {new_streak} empty results, synthesized"],
            }

        return {
            "agent_messages": step_messages,
            "agent_called": called,
            "agent_observations": observations,
            "agent_empty_streak": new_streak,
            "agent_had_data": had_data,
            "agent_large_query_families": large_families,
            "agent_iterations": iteration,
            "trail": [f"think_and_act #{iteration}: called {tool_name}" + (" (empty)" if empty else "")],
        }

    # ---- Unrecognized action ----
    return {
        "agent_messages": [
            AIMessage(content=raw_content),
            HumanMessage(content=(
                "ERROR: no valid action. Respond with JSON: "
                '{"action":"tool_call",...} or {"action":"final_answer","answer":"..."}.'
            )),
        ],
        "agent_iterations": iteration,
        "trail": [f"think_and_act #{iteration}: invalid action format"],
    }

async def agent_login(state):
    return await login(state)

