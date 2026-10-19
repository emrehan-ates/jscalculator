"""Chainlit UI — create/bind/define_port (approval) + agentic reads/troubleshooting + auth + roles + chat history + persistent approval queue. English UI.

Approval model (Seçenek 2):
- A 'user' write request is queued (persistent) and the turn ends.
- An 'admin' write request gets inline approve/reject (admin is the approver).
- Admin types `approvals` to see all pending requests and approve/deny them.
- Anyone types `my requests` to see their own requests (pending/approved/denied).
"""

import json
import os

from dotenv import load_dotenv

load_dotenv()

# Phoenix tracing — must be registered before any LangChain imports
from phoenix.otel import register
from openinference.instrumentation.langchain import LangChainInstrumentor

_tracer_provider = register(
    project_name=os.environ.get("PHOENIX_PROJECT_NAME", "aci-orchestrator"),
    endpoint=os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "http://10.6.107.105:6006"),
)
LangChainInstrumentor().instrument(tracer_provider=_tracer_provider)

import chainlit as cl
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer

from orchestrator.supervisor import prepare as prepare_request
from orchestrator.supervisor import execute_create
from orchestrator.chat_memory import (
    append_chat_turn,
    normalize_chat_history,
    reconstruct_chat_history,
)
from orchestrator.mcp_runtime import get_runtime_manager
from users import verify_user, get_admin_emails
from mailer import send_email
import approvals

_MESSAGE_TYPES = {"user_message", "assistant_message"}

# Commands (case-insensitive)
_ADMIN_QUEUE_CMDS = {"approvals", "pending", "queue", "bekleyenler", "onaylar"}
_MY_REQUESTS_CMDS = {"my requests", "my approvals", "requests", "taleplerim", "isteklerim"}
_MEMORY_EXCLUDED_COMMANDS = _ADMIN_QUEUE_CMDS | _MY_REQUESTS_CMDS
_CHAT_HISTORY_KEY = "chat_history"

class RootMessageDataLayer(SQLAlchemyDataLayer):
    async def create_step(self, step_dict):
        if step_dict.get("type") in _MESSAGE_TYPES:
            step_dict["parentId"] = None
        return await super().create_step(step_dict)

    async def update_step(self, step_dict):
        if step_dict.get("type") in _MESSAGE_TYPES:
            step_dict["parentId"] = None
        return await super().update_step(step_dict)

@cl.data_layer
def get_data_layer():
    return RootMessageDataLayer(
        conninfo="sqlite+aiosqlite:////root/langgraph-orchestrator/chat_history.db"
    )

@cl.password_auth_callback
def auth_callback(username: str, password: str):
    user = verify_user(username, password)
    if user is None:
        return None
    return cl.User(
        identifier=username,
        metadata={"role": user["role"], "email": user.get("email", ""), "team": user.get("team", "")},
    )

# NOTE: MCP server connection config now lives in orchestrator/mcp_runtime.py
# (single source of truth). The chat profile only selects which fabric dict to
# hand to the persistent runtime, which keeps that fabric's apic_client server
# and the shared aci_kb/skills servers alive across the whole app.

# ============================================================
# Fabric selection (Chainlit chat profiles)
# ============================================================
# Up to four ZT fabrics. Each fabric has its OWN APIC_CLIENT MCP server
# deployment (command + args) configured via FABRIC_<n>_* env vars. Each
# fabric becomes a chat profile the user picks at the start of a conversation;
# the chosen profile decides which apic_client MCP server is launched, and that
# server connects to its fabric's APIC using its own .env credentials.

_FABRIC_IDS = (1, 2, 3, 4)

def _fabric_config(fid: int) -> dict:
    """Read one fabric's APIC_CLIENT MCP server config from the environment."""
    def g(suffix, default=None):
        return os.environ.get(f"FABRIC_{fid}_{suffix}", default)
    return {
        "id": fid,
        "name": g("NAME") or f"Fabric {fid}",
        "description": g("DESCRIPTION") or f"ZT APIC fabric {fid}",
        "icon": g("ICON"),
        # Each fabric has its OWN APIC_CLIENT MCP server deployment.
        "apic_client_python": g("APIC_CLIENT_PYTHON", ""),
        "apic_client_server": g("APIC_CLIENT_SERVER", ""),
    }

def _all_fabrics() -> list[dict]:
    """Every fabric that has an APIC_CLIENT MCP server configured.

    A fabric is exposed when FABRIC_<n>_APIC_CLIENT_SERVER (or _NAME) is set.
    Falls back to a single 'Default Fabric' built from the legacy
    APIC_CLIENT_* vars so an existing single-fabric deployment keeps working
    with no config change.
    """
    fabrics = [
        _fabric_config(fid) for fid in _FABRIC_IDS
        if os.environ.get(f"FABRIC_{fid}_APIC_CLIENT_SERVER") or os.environ.get(f"FABRIC_{fid}_NAME")
    ]
    if not fabrics:
        fabrics.append({
            "id": 0,
            "name": os.environ.get("DEFAULT_FABRIC_NAME", "Default Fabric"),
            "description": "Default APIC fabric (single-fabric configuration).",
            "icon": None,
            "apic_client_python": os.environ.get("APIC_CLIENT_PYTHON", ""),
            "apic_client_server": os.environ.get("APIC_CLIENT_SERVER", ""),
        })
    return fabrics

def _fabric_by_name(name: str) -> dict:
    for f in _all_fabrics():
        if f["name"] == name:
            return f
    return _all_fabrics()[0]

@cl.set_chat_profiles
async def chat_profiles(current_user=None):
    profiles = []
    for f in _all_fabrics():
        kwargs = {"name": f["name"], "markdown_description": f["description"]}
        if f.get("icon"):
            kwargs["icon"] = f["icon"]
        profiles.append(cl.ChatProfile(**kwargs))
    return profiles

def _setup_session():
    profile_name = cl.user_session.get("chat_profile")
    fabric = _fabric_by_name(profile_name) if profile_name else _all_fabrics()[0]
    cl.user_session.set("fabric", fabric)
    app_user = cl.user_session.get("user")
    role = "user"
    team = ""
    if app_user and app_user.metadata:
        role = app_user.metadata.get("role", "user")
        team = app_user.metadata.get("team", "")
    cl.user_session.set("role", role)
    cl.user_session.set("team", team)
    return app_user, role

def _username():
    app_user = cl.user_session.get("user")
    return app_user.identifier if app_user else "?"

def _chat_history():
    return normalize_chat_history(cl.user_session.get(_CHAT_HISTORY_KEY) or [])

def _remember_turn(user_input: str, assistant_output: str):
    cl.user_session.set(
        _CHAT_HISTORY_KEY,
        append_chat_turn(_chat_history(), user_input, assistant_output),
    )

@cl.on_chat_start
async def on_chat_start():
    await approvals.init()
    cl.user_session.set(_CHAT_HISTORY_KEY, [])
    app_user, role = _setup_session()
    # Lazy-but-warm: pre-spawn this fabric's persistent MCP servers now so the
    # first message doesn't pay the startup cost. Best-effort — a failure here
    # (e.g. an unreachable APIC) is logged inside warm() and surfaces as a clear
    # per-request error rather than blocking chat start. Chosen over eager
    # startup of all four fabrics because it gives per-fabric fault isolation
    # for free and never blocks the app on an unreachable APIC.
    fabric = cl.user_session.get("fabric") or {}
    if fabric:
        await get_runtime_manager().warm(fabric)
    username = app_user.identifier if app_user else "?"
    team = cl.user_session.get("team", "")
    role_label = "Admin" if role == "admin" else "User"
    team_label = f" — Team: {team}" if team else ""
    fabric_label = f" — Fabric: {fabric.get('name', 'Default')}"

    extra = ""
    if role == "admin":
        extra = "\n- Type `approvals` to review pending requests from users"

    await cl.Message(content=(
        f"**ACI Agent ready.** (Logged in: {username} — {role_label}{team_label}{fabric_label})\n\n"
        "Deterministic write operations (require approval):\n"
        "- Create tenant / BD\n"
        "- Bind EPG to ports\n"
        "- Define a port (access/PC/vPC)\n\n"
        "Everything else is handled by the agent — just ask:\n"
        "- List / find / show anything (tenants, BDs, EPGs, endpoints, interfaces, faults…)\n"
        "- Troubleshoot (endpoint not learned, blocked traffic, port down, health/faults…)\n\n"
        "- Type `my requests` to see the status of your requests"
        + extra
    )).send()

@cl.on_chat_resume
async def on_chat_resume(thread):
    await approvals.init()
    cl.user_session.set(
        _CHAT_HISTORY_KEY,
        reconstruct_chat_history(
            thread.get("steps") or [],
            excluded_user_messages=_MEMORY_EXCLUDED_COMMANDS,
        ),
    )
    # Kalıcı thread metadata'sından chat profilini (fabric) geri yükle
    md = thread.get("metadata") or {}
    if isinstance(md, str):
        md = json.loads(md)
    profile = md.get("chat_profile")
    print(profile)
    if profile:
        cl.user_session.set("chat_profile", profile)
    try:
        _setup_session()
        fabric = cl.user_session.get("fabric") or {}
        if fabric:
            await get_runtime_manager().warm(fabric)
    except Exception as e:
        # Resume asla çökmemeli, yoksa thread hiç açılmaz
        cl.user_session.set("fabric", _all_fabrics()[0])
# ============================================================
# Helpers
# ============================================================

def _plan_text(state):
    payload = state.get("payload", {})
    return (
        f"**Planned operation:**\n\n"
        f"- Type: `{state.get('target_class')}`\n"
        f"- DN: `{state.get('dn')}`\n"
        f"- Payload:\n```json\n{json.dumps(payload, indent=2)}\n```\n"
    )

def _curate_plan(state):
    """Keep only what execute_create needs, and ensure it's JSON-serializable."""
    return {
        "user_input": state.get("user_input"),
        "flow": "create",
        "intent": state.get("intent"),
        "target_class": state.get("target_class"),
        "dn": state.get("dn"),
        "parent_dn": state.get("parent_dn"),
        "payload": state.get("payload"),
        "post_path": state.get("post_path"),
        "post_body": state.get("post_body"),
        "trail": state.get("trail", []),
    }

def _apic_outcome(final_result):
    """Return (ok: bool, error: str|None) from an APIC result."""
    err = None
    if isinstance(final_result, dict):
        if "error" in final_result:
            err = final_result["error"]
        imdata = final_result.get("imdata")
        for item in imdata if isinstance(imdata, list) else []:
            if isinstance(item, dict) and "error" in item:
                err = item["error"].get("attributes", {}).get("text", "APIC error")
    return (err is None, err)

_STATUS_ICON = {"pending": "⏳", "approved": "✅", "denied": "❌"}

# ============================================================
# Message handler
# ============================================================

@cl.on_message
async def on_message(message: cl.Message):
    fabric = cl.user_session.get("fabric")
    role = cl.user_session.get("role", "user")
    username = _username()
    text = (message.content or "").strip()
    low = text.lower()

    # ---- Commands: approval queue ----
    if low in _ADMIN_QUEUE_CMDS:
        if role != "admin":
            await cl.Message(
                content="Only admins can view the approval queue. Type `my requests` to see your own."
            ).send()
        else:
            await show_pending_queue()
        return

    if low in _MY_REQUESTS_CMDS:
        await show_my_requests(username)
        return

    # ---- Normal request handling ----
    team = cl.user_session.get("team", "")
    profile = cl.user_session.get("chat_profile") or ""
    try:
        state = await prepare_request(
            message.content,
            fabric,
            team=team,
            profile=profile,
            chat_history=_chat_history(),
        )
    except Exception as e:
        await cl.Message(content=f"⚠️ Unexpected error: {e}").send()
        return

    flow = state.get("flow", "unknown")
    errors = state.get("errors", [])
    trail_text = "\n".join(f"→ {t}" for t in state.get("trail", []))

    async def send_answer(content: str):
        msg = await cl.Message(content=content).send()
        if trail_text:
            async with cl.Step(name="Steps", type="tool", parent_id=msg.id) as step:
                step.output = trail_text
        return msg

    if errors and flow != "agentic":
        problem_text = "⚠️ **Problem:**\n" + "\n".join(f"- {e}" for e in errors)
        await send_answer(problem_text)
        if flow == "create" and not state.get("dn"):
            _remember_turn(text, problem_text)
            return

    if flow == "agentic":
        answer = state.get("agent_final_answer")
        if answer:
            response_text = answer
        else:
            response_text = "The agent could not produce an answer."
        await send_answer(response_text)
        _remember_turn(text, response_text)
        return

    if flow == "unknown":
        response_text = "🤔 I couldn't understand the request."
        await send_answer(response_text)
        _remember_turn(text, response_text)
        return

    if flow == "create":
        dn = state.get("dn")
        if not dn:
            response_text = "⚠️ Could not prepare a plan (no DN)."
            await send_answer(response_text)
            _remember_turn(text, response_text)
            return

        plan_text = _plan_text(state)

        if role == "admin":
            # Admin is the approver → inline immediate approval
            response_text = await handle_admin_approval(state, plan_text, fabric)
            if response_text:
                _remember_turn(text, response_text)
        else:
            # User → queue it persistently, end the turn
            summary = f"{state.get('target_class')} @ {dn}"
            app_user = cl.user_session.get("user")
            requester_email = (app_user.metadata or {}).get("email", "") if app_user else ""
            fabric = cl.user_session.get("fabric", {})
            fabric_name = fabric.get("name", " ")
            aid = await approvals.create(
                username,
                requester_email,
                message.content,
                state.get("intent", ""),
                summary,
                _curate_plan(state),
                fabric_name,
            )
            admin_emails = get_admin_emails()

            
            if admin_emails:
                payload = state.get("payload", {})
                email_body = (
                    f"A new approval request (#{aid}) has been submitted.\n\n"
                    f"Requested By : {username}\n"
                    f"Fabric       : {fabric_name}\n"
                    f"Request      : {message.content}\n"
                    f"Type         : {state.get('target_class')}\n"
                    f"DN:          : {dn}\n"
                    f"Payload      : \n{json.dumps(payload, indent=2)}\n"
                    "Login to http://10.6.107.105:8001 and type 'approvals' to review"
                )
                await send_email(
                    admin_emails,
                    f"[NetworkGPT] New approval request #{aid} from {username}",
                    email_body,
                )
            response_text = (
                plan_text
                + f"\n⏳ **Submitted for admin approval as request #{aid}.**\n"
                "An admin will review it. Type `my requests` to check the status."
            )
            await send_answer(response_text)
            _remember_turn(text, response_text)
        return

# ============================================================
# Admin inline approval (admin's own write requests)
# ============================================================

async def handle_admin_approval(state, plan_text, fabric):
    actions = [
        cl.Action(name="approve", payload={"value": "approve"}, label="✅ Approve"),
        cl.Action(name="reject", payload={"value": "reject"}, label="❌ Reject"),
    ]
    res = await cl.AskActionMessage(
        content=plan_text + "\nDo you approve sending this to APIC?",
        actions=actions,
        timeout=300,
    ).send()

    if res is None:
        response_text = "⏱️ Timed out — operation cancelled."
        await cl.Message(content=response_text).send()
        return response_text
    if res.get("payload", {}).get("value") == "reject":
        response_text = "❌ Operation cancelled. Nothing was sent to APIC."
        await cl.Message(content=response_text).send()
        return response_text

    try:
        final_state = await execute_create(state, fabric)
    except Exception as e:
        response_text = f"⚠️ Send error: {e}"
        await cl.Message(content=response_text).send()
        return response_text
    return await render_create_result(final_state)

# ============================================================
# Approval queue views + actions
# ============================================================

async def show_pending_queue():
    pending = await approvals.list_pending()
    if not pending:
        await cl.Message(content="✅ No pending requests.").send()
        return
    await cl.Message(content=f"**{len(pending)} pending request(s):**").send()
    for a in pending:
        plan = json.loads(a["plan_json"])
        body = (
            f"**Request #{a['id']}** — from `{a['requester']}`\n"
            f"- Fabric: `{a.get('fabric', '')}`\n"
            f"- Request: {a['user_input']}\n"
            f"- Type: `{plan.get('target_class')}`\n"
            f"- DN: `{plan.get('dn')}`\n"
            f"- Payload:\n```json\n{json.dumps(plan.get('payload', {}), indent=2)}\n```"
        )
        actions = [
            cl.Action(name="approve_request", payload={"id": a["id"]}, label="✅ Approve"),
            cl.Action(name="deny_request", payload={"id": a["id"]}, label="❌ Reject"),
        ]
        await cl.Message(content=body, actions=actions).send()

async def show_my_requests(username):
    rows = await approvals.list_by_requester(username)
    if not rows:
        await cl.Message(content="You have no requests yet.").send()
        return
    lines = [f"**Your requests ({len(rows)}):**\n"]
    for a in rows:
        icon = _STATUS_ICON.get(a["status"], "•")
        line = f"{icon} **#{a['id']}** — {a['status']} — `{a.get('summary') or ''}`"
        if a["status"] == "approved" and a.get("result_json"):
            try:
                r = json.loads(a["result_json"])
                if r.get("apic_ok"):
                    line += " — applied ✅"
                else:
                    line += f" — APIC error: {r.get('error')}"
            except Exception:
                pass
        if a.get("decided_by"):
            line += f"  _(by {a['decided_by']})_"
        lines.append(line)
    await cl.Message(content="\n".join(lines)).send()

@cl.action_callback("approve_request")
async def on_approve_request(action: cl.Action):
    role = cl.user_session.get("role", "user")
    if role != "admin":
        await cl.Message(content="Only admins can approve requests.").send()
        return
    approval_id = action.payload.get("id")
    appr = await approvals.get(approval_id)
    if not appr:
        await cl.Message(content=f"Request #{approval_id} not found.").send()
        return
    if appr["status"] != "pending":
        await cl.Message(content=f"Request #{approval_id} is already {appr['status']}.").send()
        return

    plan = json.loads(appr["plan_json"])
    fabric = _fabric_by_name(appr.get("fabric") or "")
    if fabric["name"] != (appr.get("fabric") or fabric["name"]):
        await cl.Message(content=f"⚠️ Fabric '{appr.get('fabric')}' artık tanımlı değil.").send()
        return
    admin = _username()

    await cl.Message(content=f"Processing request #{approval_id}…").send()
    try:
        final_state = await execute_create(plan, fabric)
    except Exception as e:
        await approvals.set_decision(
            approval_id, "approved", admin, {"apic_ok": False, "error": str(e)}
        )
        await cl.Message(
            content=f"⚠️ Request #{approval_id} approved but execution failed: {e}"
        ).send()
        return

    ok, err = _apic_outcome(final_state.get("final_result", {}))
    await approvals.set_decision(
        approval_id, "approved", admin, {"apic_ok": ok, "error": err}
    )
    await cl.Message(content=f"Request #{approval_id} approved by `{admin}`.").send()
    await render_create_result(final_state)

    requester_email = appr.get("requester_email") or ""
    if requester_email:
        if ok:
            result_note = "Your request has been applied successfully to APIC."
        else:
            result_note = f"Your request was approved but APIC returned an error: {err}"
        await send_email(
            requester_email,
            f"[NetworkGPT] Your request #{approval_id} has been approved",
            (
                f"Hello {appr['requester']},\n\n"
                f"Your request #{approval_id} has been approved by {admin}.\n\n"
                f"Request  : {appr['user_input']}\n"
                f"Fabric   : {appr.get('fabric', '')}\n"
                f"Summary  : {appr.get('summary', '')}\n\n"
                f"{result_note}\n\n"
                "You can check the status at http://10.6.107.105:8001 by typing 'my requests'."
            ),
        )

@cl.action_callback("deny_request")
async def on_deny_request(action: cl.Action):
    role = cl.user_session.get("role", "user")
    if role != "admin":
        await cl.Message(content="Only admins can deny requests.").send()
        return
    approval_id = action.payload.get("id")
    appr = await approvals.get(approval_id)
    if not appr:
        await cl.Message(content=f"Request #{approval_id} not found.").send()
        return
    if appr["status"] != "pending":
        await cl.Message(content=f"Request #{approval_id} is already {appr['status']}.").send()
        return
    admin = _username()
    await approvals.set_decision(approval_id, "denied", admin, None)
    await cl.Message(
        content=f"❌ Request #{approval_id} denied by `{admin}`. Nothing was sent to APIC."
    ).send()

    requester_email = appr.get("requester_email") or ""
    if requester_email:
        await send_email(
            requester_email,
            f"[NetworkGPT] Your request #{approval_id} has been rejected",
            (
                f"Hello {appr['requester']},\n\n"
                f"Your request #{approval_id} has been rejected by {admin}.\n\n"
                f"Request  : {appr['user_input']}\n"
                f"Summary  : {appr.get('summary', '')}\n\n"
                "If you have questions, please contact the network admin team.\n\n"
                "You can check the status at http://10.6.107.105:8001 by typing 'my requests'."
            ),
        )

async def render_create_result(state):
    dn = state.get("dn")
    final_result = state.get("final_result", {})
    interpretation = state.get("interpretation", "")
    trail_text = "\n".join(f"→ {t}" for t in state.get("trail", []))

    ok, apic_error = _apic_outcome(final_result)
    if not ok:
        body = f"❌ **APIC rejected the operation.**\n\nDN: `{dn}`\nError: {apic_error}"
    else:
        body = f"✅ **Successfully applied.**\n\nType: `{state.get('target_class')}`\nDN: `{dn}`"
    if interpretation:
        body += f"\n\n{interpretation}"

    msg = await cl.Message(content=body).send()
    if trail_text:
        async with cl.Step(name="Steps", type="tool", parent_id=msg.id) as step:
            step.output = trail_text
    return body

