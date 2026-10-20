"""Multi-task requests: split one message into atomic tasks, prepare every
create into ONE approval plan, apply it in as few POSTs as possible.

Flow (driven by chainlit_app.on_message):
  1. decompose_request()   one LLM call -> ordered tasks
                           [{id, intent, flow, params, target_class, summary,
                             depends_on, question?}]; at most MAX_TASKS.
  2. The user confirms the task list (only when it contains a create).
  3. Agentic tasks run one by one (supervisor.run_agentic_task).
  4. prepare_create_batch() prepares the creates in order. Any preparation
     error stops the whole batch — nothing goes to approval.
  5. On approval, execute_batch() applies stage 0 as ONE polUni POST (APIC
     applies a single POST all-or-nothing), then each later stage.

Dependencies between creates:
  - KNOWN (prepared from the plan, same POST): a port defined in this request
    used by bind_epg / bind_vmm_domain / copy_epg_bindings (its path is
    computed from the definition), and create_bd in a tenant created here.
  - anything else: the dependent task moves to a later STAGE; it is prepared
    against live APIC data right after the earlier stage is applied. If that
    preparation or its POST fails, execution stops and reports what was and
    was not applied (earlier stages stay applied).
"""
import json
import re

from .nodes_common import (INTENT_SYSTEM_PROMPT, _intent_messages,
                           _current_request_authorizes_write, extract_json_from_llm,
                           get_llm, get_tool, unwrap_mcp_result)
from .nodes_port import planned_port_paths, reset_planned_ports, set_planned_ports

MAX_TASKS = 20
PORT_DEFINERS = {"define_port", "copy_port_definition"}
PORT_CONSUMERS = {"bind_epg", "bind_vmm_domain", "copy_epg_bindings"}
KNOWN_DEPS = ({(d, c) for d in PORT_DEFINERS for c in PORT_CONSUMERS}
              | {("create_tenant", "create_bd")})

BATCH_PROMPT_SUFFIX = """

MULTI-TASK OUTPUT — this OVERRIDES the single-object response format above.
The request may contain SEVERAL operations (creates and/or questions). Split
it into ATOMIC tasks, in the order the user gave them, and respond with ONLY:
{"tasks": [{"id": 1, "intent": "<intent>", "flow": "create"|"agentic",
            "params": {...}, "target_class": "<class or empty>",
            "summary": "<one short line in the user's language>",
            "depends_on": []}, ...]}

Rules:
- Each create task is ONE intent from the lists above with its own params,
  following the PARAM RULES exactly (never invent values).
- Do NOT split an operation that already takes lists: one bind_epg with
  several EPGs and ports, one copy_epg_bindings with many destinations, one
  remove_from_dvs with many servers. Different operations are separate tasks.
- Agentic tasks: "intent": "agentic", "params": {}, plus "question": a
  self-contained question that names every object it needs. Keep questions
  that depend on each other's answers (e.g. "find X's ports, then list the
  EPGs on them") in ONE agentic task.
- "depends_on": ids of EARLIER create tasks whose created object this task
  uses (bind_epg to a port that an earlier define_port creates; create_bd in
  a tenant an earlier create_tenant creates). Creates never depend on agentic
  tasks. When a task uses an object an earlier task creates, refer to it by
  the SAME name (e.g. the define_port "name" as the bind_epg "serial").
- A request with a single operation returns a list with ONE task.
"""


def _norm_task(raw, idx, user_input):
    if not isinstance(raw, dict):
        return None
    intent = str(raw.get("intent") or "agentic").strip()
    flow = "agentic" if intent == "agentic" else (raw.get("flow") or "create")
    flow = flow if flow in ("create", "agentic") else "agentic"
    if flow == "agentic":
        intent = "agentic"
    try:
        tid = int(raw.get("id", idx))
    except (TypeError, ValueError):
        tid = idx
    deps = raw.get("depends_on") or []
    if not isinstance(deps, list):
        deps = [deps]
    task = {
        "id": tid, "intent": intent, "flow": flow,
        "params": raw.get("params") if isinstance(raw.get("params"), dict) else {},
        "target_class": raw.get("target_class") or "",
        "summary": str(raw.get("summary") or raw.get("question") or intent).strip(),
        "depends_on": [d for d in deps if isinstance(d, int) or str(d).isdigit()],
    }
    if flow == "agentic":
        task["question"] = str(raw.get("question") or user_input).strip()
    return task


def _renumber(tasks):
    """Ids 1..n in order; depends_on keeps only EARLIER create tasks."""
    old_to_new = {}
    for i, t in enumerate(tasks, 1):
        old_to_new.setdefault(int(t["id"]), i)
        t["id"] = i
    flows = {t["id"]: t["flow"] for t in tasks}
    for t in tasks:
        deps = []
        for d in t["depends_on"]:
            nd = old_to_new.get(int(d))
            if nd and nd < t["id"] and flows.get(nd) == "create" and nd not in deps:
                deps.append(nd)
        t["depends_on"] = deps if t["flow"] == "create" else []
    return tasks


def assign_stages(tasks):
    """stage 0 = first POST. A task joins its dependency's stage when the
    dependency is KNOWN (resolvable from the plan), else the next stage."""
    by_id = {t["id"]: t for t in tasks}
    for t in tasks:
        if t["flow"] != "create":
            continue
        stage = 0
        for d in t["depends_on"]:
            dep = by_id[d]
            same = (dep["intent"], t["intent"]) in KNOWN_DEPS
            stage = max(stage, dep["stage"] + (0 if same else 1))
        t["stage"] = stage
    return tasks


async def decompose_request(user_input, chat_history=None, convention=None):
    """Return {"tasks": [...], "errors": [...], "trail": [...]}."""
    state = {"user_input": user_input, "chat_history": chat_history or [],
             "convention": convention}
    messages = _intent_messages(state, system_prompt=INTENT_SYSTEM_PROMPT + BATCH_PROMPT_SUFFIX)
    single_agentic = {"tasks": [{"id": 1, "intent": "agentic", "flow": "agentic", "params": {},
                                 "target_class": "", "summary": user_input,
                                 "question": user_input, "depends_on": []}],
                      "errors": [], "trail": []}
    try:
        response = await get_llm().ainvoke(messages)
        parsed = extract_json_from_llm(response.content)
    except (ValueError, json.JSONDecodeError) as e:
        single_agentic["trail"] = [f"decompose: parse failed ({e}) -> one agentic task"]
        return single_agentic

    if isinstance(parsed, dict) and isinstance(parsed.get("tasks"), list):
        raw_tasks = parsed["tasks"]
    elif isinstance(parsed, dict) and parsed.get("intent"):
        raw_tasks = [parsed]  # model answered in the single-intent format
    elif isinstance(parsed, list):
        raw_tasks = parsed
    else:
        raw_tasks = []
    tasks = [t for t in (_norm_task(r, i, user_input) for i, r in enumerate(raw_tasks, 1)) if t]
    if not tasks:
        single_agentic["trail"] = ["decompose: no tasks -> one agentic task"]
        return single_agentic

    # A bare "yes / evet yap" turn may never start a write by itself.
    if (any(t["flow"] == "create" for t in tasks) and chat_history
            and not _current_request_authorizes_write(user_input)):
        single_agentic["trail"] = ["decompose: bare affirmation cannot inherit write -> agentic"]
        return single_agentic

    if len(tasks) > MAX_TASKS:
        return {"tasks": [], "trail": [f"decompose: {len(tasks)} tasks > {MAX_TASKS}"],
                "errors": [f"This request contains {len(tasks)} operations; the limit is "
                           f"{MAX_TASKS} per message. Please split it into smaller requests."]}

    tasks = assign_stages(_renumber(tasks))
    trail = [f"decompose: {len(tasks)} task(s): "
             + ", ".join(f"#{t['id']} {t['intent']}"
                         + (f"(after {t['depends_on']}, stage {t['stage']})" if t.get("depends_on") else "")
                         for t in tasks)]
    return {"tasks": tasks, "errors": [], "trail": trail}


# ---------------------------------------------------------------------------
# Merging prepared creates into one polUni POST
# ---------------------------------------------------------------------------

def _poluni_children(prep):
    """The polUni children equivalent to one prepared create."""
    path, body = prep.get("post_path") or "", prep.get("post_body")
    if body:
        if path.endswith("/mo/uni.json") and "polUni" in body:
            return list(body["polUni"].get("children", []))
        if path.endswith("/mo/uni/infra.json") and "infraInfra" in body:
            return [body]
        raise ValueError(f"cannot merge a POST to {path}")
    cls, attrs = prep.get("target_class"), dict(prep.get("payload") or {})
    node = {cls: {"attributes": attrs}}
    parent = prep.get("parent_dn") or ""
    if parent in ("", "uni"):
        return [node]
    m = re.fullmatch(r"uni/tn-([^/]+)", parent)
    if m:
        return [{"fvTenant": {"attributes": {"name": m.group(1)}, "children": [node]}}]
    raise ValueError(f"cannot merge {cls} under {parent}")


def _key(node):
    (cls, body), = node.items()
    a = body.get("attributes", {})
    return cls, a.get("name") or a.get("tDn") or a.get("rn") or ""


def merge_children(children):
    """Merge sibling MOs with the same class + identity (recursively), so the
    same tenant/AP/EPG/infra node appears once in the combined tree."""
    out, index = [], {}
    for node in children:
        (cls, body), = node.items()
        k = _key(node)
        if k not in index:
            index[k] = {cls: {"attributes": dict(body.get("attributes", {})),
                              "children": list(body.get("children", []))}}
            out.append(index[k])
            continue
        tgt = index[k][cls]
        tgt["attributes"].update(body.get("attributes", {}))
        tgt["children"].extend(body.get("children", []))
    for node in out:
        (_, body), = node.items()
        if body["children"]:
            body["children"] = merge_children(body["children"])
        else:
            body.pop("children")
    return out


def merge_preps(preps):
    children = []
    for p in preps:
        children.extend(_poluni_children(p))
    return {"polUni": {"attributes": {}, "children": merge_children(children)}}


# ---------------------------------------------------------------------------
# Preparation and execution
# ---------------------------------------------------------------------------

async def _prepare_stage(tasks, user_input, team, convention):
    """Prepare tasks in order. Ports defined by earlier tasks of the SAME
    stage are visible to later ones. Returns (preps, errors)."""
    from .supervisor import prepare_create_task
    planned, preps, errors = {}, [], []
    for t in tasks:
        token = set_planned_ports(dict(planned))
        try:
            prep = await prepare_create_task(t, user_input, team=team, convention=convention)
        except Exception as e:
            prep = {"errors": [f"unexpected error: {e}"]}
        finally:
            reset_planned_ports(token)
        if prep.get("errors"):
            errors.extend(f"Task {t['id']} ({t['summary']}): {e}" for e in prep["errors"])
            continue
        prep["task"] = {"id": t["id"], "summary": t["summary"], "intent": t["intent"]}
        pp = prep.get("planned_port")
        if pp:
            ports = pp["ports"] if isinstance(pp["ports"], list) else [pp["ports"]]
            planned[pp["name"]] = {"kind": pp["type"], "nodes": [str(n) for n in pp["nodes"]],
                                   "tdns": planned_port_paths(pp["name"], pp["type"], pp["nodes"], ports)}
        preps.append(prep)
    return preps, errors


def _section(prep):
    return {"task": prep["task"]["id"], "summary": prep["task"]["summary"],
            "operation": prep.get("target_class") or prep["task"]["intent"],
            "details": prep.get("payload")}


async def prepare_create_batch(tasks, user_input, team="", convention=None):
    """One prepared-state for all create tasks (approval plan).

    A request with a single create returns that create's own prepared state,
    so the plan looks exactly as before."""
    creates = [t for t in tasks if t["flow"] == "create"]
    now = [t for t in creates if t.get("stage", 0) == 0]
    later = [t for t in creates if t.get("stage", 0) > 0]

    preps, errors = await _prepare_stage(now, user_input, team, convention)
    if errors:
        return {"flow": "create", "intent": "batch", "user_input": user_input,
                "errors": errors + ["Nothing was sent for approval. Fix the tasks above and "
                                    "send the request again."],
                "trail": [f"batch: {len(errors)} task(s) could not be prepared"]}

    trail = [t for p in preps for t in p.get("trail", [])]
    if len(preps) == 1 and not later:
        single = dict(preps[0])
        single.pop("task", None)
        single.pop("planned_port", None)
        return single

    try:
        body = merge_preps(preps)
    except ValueError as e:
        return {"flow": "create", "intent": "batch", "user_input": user_input,
                "errors": [f"Could not combine the tasks into one change: {e}"],
                "trail": trail + ["batch: merge failed"]}

    stages = sorted({t["stage"] for t in later})
    payload = [_section(p) for p in preps]
    for t in later:
        payload.append({"task": t["id"], "summary": t["summary"], "operation": t["intent"],
                        "stage": t["stage"] + 1,
                        "details": "prepared with live APIC data after the previous stage "
                                   "is applied", "params": t["params"]})
    n = len(creates)
    return {
        "flow": "create", "intent": "batch", "user_input": user_input,
        "target_class": f"batch ({n} change(s)" + (f", {len(stages) + 1} stages)" if stages else ")"),
        "dn": "uni",
        "payload": payload,
        "post_path": "/api/node/mo/uni.json",
        "post_body": body,
        "batch": {"team": team, "convention": convention,
                  "stage0_tasks": [p["task"] for p in preps],
                  "pending": later},
        "errors": [],
        "trail": trail + [f"batch: {len(preps)} task(s) in stage 1"
                          + (f", {len(later)} task(s) in later stage(s)" if later else "")],
    }


def _apic_error(result):
    if isinstance(result, dict):
        if result.get("error"):
            return str(result["error"])
        for item in result.get("imdata", []) if isinstance(result.get("imdata"), list) else []:
            if isinstance(item, dict) and "error" in item:
                return item["error"].get("attributes", {}).get("text", "APIC error")
    return None


async def _post(path, body):
    raw = await get_tool("apic_post").ainvoke({"path": path, "payload": body})
    return unwrap_mcp_result(raw)


async def execute_batch(plan):
    """Apply an approved batch plan stage by stage; stop at the first failure."""
    batch = plan.get("batch") or {}
    user_input = plan.get("user_input", "")
    lines, trail, results = [], [], []
    await get_tool("apic_login").ainvoke({})

    def label(tasks):
        return ", ".join(f"#{t['id']} {t['summary']}" for t in tasks)

    stage_tasks = batch.get("stage0_tasks", [])
    result = await _post(plan["post_path"], plan["post_body"])
    err = _apic_error(result)
    results.append({"stage": 1, "tasks": [t["id"] for t in stage_tasks], "ok": err is None, "error": err})
    trail.append(f"batch stage 1: POST {'ok' if err is None else 'FAILED'}")
    if err:
        lines.append(f"Stage 1 FAILED — APIC rejected the change, nothing was applied: {err}")
    else:
        lines.append(f"Stage 1 applied: {label(stage_tasks)}")
        pending = batch.get("pending", [])
        for k, stage in enumerate(sorted({t["stage"] for t in pending}), start=2):
            tasks = [t for t in pending if t["stage"] == stage]
            preps, errors = await _prepare_stage(tasks, user_input, batch.get("team", ""),
                                                 batch.get("convention"))
            if errors:
                err = "; ".join(errors)
                result = {"error": f"stage {k} could not be prepared: {err}"}
                results.append({"stage": k, "tasks": [t["id"] for t in tasks], "ok": False, "error": err})
                lines.append(f"Stage {k} NOT applied — preparation failed: {err}")
                break
            result = await _post("/api/node/mo/uni.json", merge_preps(preps))
            err = _apic_error(result)
            results.append({"stage": k, "tasks": [t["id"] for t in tasks], "ok": err is None, "error": err})
            trail.extend(t for p in preps for t in p.get("trail", []))
            trail.append(f"batch stage {k}: POST {'ok' if err is None else 'FAILED'}")
            if err:
                lines.append(f"Stage {k} FAILED — APIC rejected it: {err}")
                break
            lines.append(f"Stage {k} applied: {label(tasks)}")
        if err:
            done = [r["stage"] for r in results if r["ok"]]
            lines.append(f"Stopped. Applied stage(s): {done}. Later stages were not sent.")

    return {**plan, "final_result": result, "batch_results": results,
            "batch_summary": "\n".join(lines), "trail": plan.get("trail", []) + trail}
