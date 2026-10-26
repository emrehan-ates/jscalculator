"""Chainlit UI — create/bind/define_port (approval) + agentic reads/troubleshooting + auth + roles + chat history + persistent approval queue. English UI.

Auth: users live in a SQLite store with bcrypt hashes (users.py); the admin
comes from ADMIN_* in .env. Login issues an app JWT (tokens.py) that is
verified on every message/action and is the only source of username, role,
team and e-mail. New users request access at /signup (signup.py); admins type
`signups` to approve or reject them.

Approval model (Seçenek 2):
- A 'user' write request is queued (persistent) and the turn ends.
- An 'admin' write request gets inline approve/reject (admin is the approver).
- Admin types `approvals` to see all pending requests and approve/deny them.
- Anyone types `my requests` to see their own requests (pending/approved/denied).
"""

import asyncio
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
from chainlit.server import app as chainlit_server

from orchestrator.supervisor import (decompose as decompose_request, epg_admin_choices,
                                     execute_create, prepare_creates, run_agentic_task)
from orchestrator.epg_define import SUBNET_SCOPES, change_pool, validate_subnet
from orchestrator.vlan_api import VlanApiError, list_pools
from orchestrator.chat_memory import (
    append_chat_turn,
    normalize_chat_history,
    reconstruct_chat_history,
)
from orchestrator.mcp_runtime import get_runtime_manager
from mailer import send_email
import approvals
import signup
import tokens
import users

users.init()
signup.register(chainlit_server)

_MESSAGE_TYPES = {"user_message", "assistant_message"}

# Commands (case-insensitive)
_ADMIN_QUEUE_CMDS = {"approvals", "pending", "queue", "bekleyenler", "onaylar"}
_MY_REQUESTS_CMDS = {"my requests", "my approvals", "requests", "taleplerim", "isteklerim"}
_SIGNUP_CMDS = {"signups", "signup requests", "kayıtlar", "kayit talepleri", "kayıt talepleri"}
_USERS_CMDS = {"users", "kullanıcılar", "kullanicilar"}
_MEMORY_EXCLUDED_COMMANDS = _ADMIN_QUEUE_CMDS | _MY_REQUESTS_CMDS | _SIGNUP_CMDS | _USERS_CMDS
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
    # Only active users with a correct password get in. The app JWT is the
    # single carrier of role/team/e-mail; nothing else is trusted from metadata.
    user = users.verify_user(username, password)
    if user is None:
        return None
    return cl.User(identifier=user["username"], metadata={"token": tokens.issue_token(user)})

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

def _session():
    """Verify the current user's app JWT and return its claims, or None.

    Called on chat start and again on every message/action, so an expired
    token, a disabled user or a changed role/team/password (token_version)
    takes effect immediately. The verified claims are mirrored into the
    Chainlit user_session for the rest of the turn.
    """
    app_user = cl.user_session.get("user")
    token = (app_user.metadata or {}).get("token") if app_user else None
    try:
        claims = tokens.verify_token(token)
    except tokens.InvalidSession as e:
        cl.user_session.set("session_error", str(e))
        cl.user_session.set("role", "none")
        return None
    cl.user_session.set("session_error", None)
    cl.user_session.set("username", claims["username"])
    cl.user_session.set("role", claims["role"])
    cl.user_session.set("team", claims["team"])
    cl.user_session.set("email", claims["email"])
    return claims

async def _require_session(admin=False):
    """Verified claims, or None after telling the user why."""
    claims = _session()
    if claims is None:
        reason = cl.user_session.get("session_error") or "invalid session"
        await cl.Message(
            content=f"🔒 Your session is not valid ({reason}). Please log out and log in again."
        ).send()
        return None
    if admin and claims["role"] != "admin":
        await cl.Message(content="Only admins can do this.").send()
        return None
    return claims

def _setup_session():
    profile_name = cl.user_session.get("chat_profile")
    fabric = _fabric_by_name(profile_name) if profile_name else _all_fabrics()[0]
    cl.user_session.set("fabric", fabric)
    app_user = cl.user_session.get("user")
    claims = _session()
    return app_user, (claims["role"] if claims else "none")

def _username():
    return cl.user_session.get("username") or "?"

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
    if role == "none":
        await _require_session()
        return
    # Lazy-but-warm: pre-spawn this fabric's persistent MCP servers now so the
    # first message doesn't pay the startup cost. Best-effort — a failure here
    # (e.g. an unreachable APIC) is logged inside warm() and surfaces as a clear
    # per-request error rather than blocking chat start. Chosen over eager
    # startup of all four fabrics because it gives per-fabric fault isolation
    # for free and never blocks the app on an unreachable APIC.
    fabric = cl.user_session.get("fabric") or {}
    if fabric:
        await get_runtime_manager().warm(fabric)
    username = _username()
    team = cl.user_session.get("team", "")
    role_label = "Admin" if role == "admin" else "User"
    team_label = f" — Team: {team}" if team else ""
    fabric_label = f" — Fabric: {fabric.get('name', 'Default')}"

    extra = ""
    if role == "admin":
        extra = ("\n- Type `approvals` to review pending requests from users"
                 "\n- Type `signups` to approve or reject new accounts, `users` to list accounts")

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
    if state.get("intent") == "batch":
        parts = [f"**Planned operation:** `{state.get('target_class')}` — applied together "
                 "(one APIC transaction per stage; a failing stage stops the rest).\n"]
        for sec in payload:
            stage = f" — stage {sec['stage']}" if sec.get("stage") else ""
            details = sec.get("params") if sec.get("stage") else sec.get("details")
            parts.append(f"**Task {sec['task']} — {sec['summary']}** (`{sec['operation']}`{stage})")
            if sec.get("stage"):
                parts.append(f"_{sec['details']}_")
            parts.append(f"```json\n{json.dumps(details, indent=2, ensure_ascii=False)}\n```")
        return "\n".join(parts) + "\n"
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
        "batch": state.get("batch"),
        "epg_requests": state.get("epg_requests"),
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
    claims = await _require_session()
    if claims is None:
        return
    fabric = cl.user_session.get("fabric")
    role = claims["role"]
    username = claims["username"]
    text = (message.content or "").strip()
    low = text.lower()

    # ---- Commands: account administration (admin) ----
    if low in _SIGNUP_CMDS or low in _USERS_CMDS:
        if role != "admin":
            await cl.Message(content="Only admins can manage accounts.").send()
        elif low in _SIGNUP_CMDS:
            await show_pending_signups()
        else:
            await show_users()
        return

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

    # ---- Normal request handling: split into atomic tasks ----
    team = claims["team"]
    profile = cl.user_session.get("chat_profile") or ""
    history = _chat_history()
    try:
        decomp = await decompose_request(text, fabric, team=team, profile=profile,
                                         chat_history=history)
    except Exception as e:
        await cl.Message(content=f"⚠️ Unexpected error: {e}").send()
        return
    if decomp.get("errors"):
        response_text = "⚠️ " + "\n".join(decomp["errors"])
        await cl.Message(content=response_text).send()
        _remember_turn(text, response_text)
        return

    tasks = decomp["tasks"]
    creates = [t for t in tasks if t["flow"] == "create"]
    if creates and not await _confirm_tasks(tasks):
        response_text = "❌ Cancelled. Nothing was run or sent to APIC."
        await cl.Message(content=response_text).send()
        _remember_turn(text, response_text)
        return

    # Read / troubleshooting tasks: one agent run each, in order.
    parts = []
    numbered = len(tasks) > 1
    for t in (t for t in tasks if t["flow"] == "agentic"):
        try:
            st = await run_agentic_task(t["question"], fabric, chat_history=history)
            answer = st.get("agent_final_answer") or "The agent could not produce an answer."
            trail = st.get("trail", [])
        except Exception as e:
            answer, trail = f"⚠️ Unexpected error: {e}", []
        if numbered:
            answer = f"**{t['id']}) {t['summary']}**\n\n{answer}"
        await _send_with_trail(answer, decomp.get("trail", []) + trail)
        parts.append(answer)

    # Create tasks: one approval plan for all of them.
    if creates:
        try:
            state = await prepare_creates(tasks, text, fabric, team=team, profile=profile)
        except Exception as e:
            state = {"flow": "create", "errors": [f"Unexpected error: {e}"], "trail": []}
        parts.append(await _handle_create_plan(state, text, claims))

    _remember_turn(text, "\n\n".join(p for p in parts if p))


async def _send_with_trail(content: str, trail):
    msg = await cl.Message(content=content).send()
    if trail:
        async with cl.Step(name="Steps", type="tool", parent_id=msg.id) as step:
            step.output = "\n".join(f"→ {t}" for t in trail)
    return msg


async def _confirm_tasks(tasks) -> bool:
    """Show the parsed task list and ask the user to continue (only called
    when the request contains at least one create)."""
    lines = [f"**I understood {len(tasks)} task(s):**\n"]
    for t in tasks:
        icon = "🛠️" if t["flow"] == "create" else "🔎"
        after = f" — after task {', '.join(map(str, t['depends_on']))}" if t.get("depends_on") else ""
        kind = f"`{t['intent']}`" if t["flow"] == "create" else "question"
        lines.append(f"{t['id']}. {icon} {t['summary']} ({kind}{after})")
    lines.append("\nQuestions are answered first; all changes are then prepared together "
                 "and sent for approval as ONE request. Continue?")
    res = await cl.AskActionMessage(
        content="\n".join(lines),
        actions=[cl.Action(name="tasks_ok", payload={"value": "continue"}, label="▶️ Continue"),
                 cl.Action(name="tasks_cancel", payload={"value": "cancel"}, label="❌ Cancel")],
        timeout=300,
    ).send()
    return bool(res) and res.get("payload", {}).get("value") == "continue"


async def _handle_create_plan(state, text, claims) -> str:
    """Show a prepared create plan and route it to approval. Returns the text
    shown, for chat memory."""
    fabric = cl.user_session.get("fabric") or {}
    username = claims["username"]
    errors = state.get("errors", [])
    trail = state.get("trail", [])

    if errors:
        problem_text = "⚠️ **Problem — nothing was sent for approval:**\n" + "\n".join(f"- {e}" for e in errors)
        await _send_with_trail(problem_text, trail)
        return problem_text

    dn = state.get("dn")
    if not dn:
        response_text = "⚠️ Could not prepare a plan (no DN)."
        await _send_with_trail(response_text, trail)
        return response_text

    plan_text = _plan_text(state)
    if claims["role"] == "admin":
        # Admin is the approver → inline immediate approval
        return await handle_admin_approval(state, plan_text, fabric) or ""

    # User → queue it persistently, end the turn
    summary = f"{state.get('target_class')} @ {dn}"
    fabric_name = fabric.get("name", " ")
    aid = await approvals.create(
        username, claims["email"], text, state.get("intent", ""), summary,
        _curate_plan(state), fabric_name,
    )
    admin_emails = users.get_admin_emails()
    if admin_emails:
        email_body = (
            f"A new approval request (#{aid}) has been submitted.\n\n"
            f"Requested By : {username}\n"
            f"Fabric       : {fabric_name}\n"
            f"Request      : {text}\n"
            f"Type         : {state.get('target_class')}\n"
            f"DN:          : {dn}\n"
            f"Payload      : \n{json.dumps(state.get('payload', {}), indent=2, ensure_ascii=False)}\n"
            "Login to http://10.6.107.105:8001 and type 'approvals' to review"
        )
        await send_email(admin_emails, f"[NetworkGPT] New approval request #{aid} from {username}",
                         email_body)
    response_text = (
        plan_text
        + f"\n⏳ **Submitted for admin approval as request #{aid}.**\n"
        "An admin will review it. Type `my requests` to check the status."
    )
    await _send_with_trail(response_text, trail)
    return response_text

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
    if state.get("epg_requests") and not await _collect_epg_admin_input(state, fabric):
        response_text = "❌ EPG definition cancelled. Nothing was sent to APIC."
        await cl.Message(content=response_text).send()
        return response_text
    state["approval_ref"] = f"by {_username()}"

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
    if await _require_session(admin=True) is None:
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

    if plan.get("epg_requests"):
        if not await _collect_epg_admin_input(plan, fabric):
            await cl.Message(content=f"Approval of request #{approval_id} cancelled — "
                                     "it is still pending.").send()
            return
        appr = await approvals.get(approval_id)
        if not appr or appr["status"] != "pending":
            await cl.Message(content=f"Request #{approval_id} was decided meanwhile.").send()
            return
    plan["approval_ref"] = f"#{approval_id}"

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
    if await _require_session(admin=True) is None:
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


# ============================================================
# Account administration: signup approvals (admin)
# ============================================================

async def show_pending_signups():
    pending = await asyncio.to_thread(users.list_users, "pending")
    if not pending:
        await cl.Message(content="✅ No pending signup requests.").send()
        return
    await cl.Message(content=f"**{len(pending)} pending signup request(s):**").send()
    for u in pending:
        body = (
            f"**{u['username']}**\n"
            f"- E-mail: {u['email'] or '-'}\n"
            f"- Team: `{u['team'] or '-'}`\n"
            f"- Requested: {u['created_at'][:16].replace('T', ' ')} UTC"
        )
        actions = [
            cl.Action(name="approve_signup", payload={"username": u["username"], "role": "user"},
                      label="✅ Approve"),
            cl.Action(name="approve_signup", payload={"username": u["username"], "role": "admin"},
                      label="🛡️ Approve as admin"),
            cl.Action(name="reject_signup", payload={"username": u["username"]}, label="❌ Reject"),
        ]
        await cl.Message(content=body, actions=actions).send()

async def show_users():
    rows = await asyncio.to_thread(users.list_users)
    lines = [f"**Accounts ({len(rows)}):**\n",
             "| User | Role | Team | Status | Last login |", "|---|---|---|---|---|"]
    for u in rows:
        last = (u.get("last_login") or "-")[:16].replace("T", " ")
        lines.append(f"| {u['username']} | {u['role']} | {u['team'] or '-'} | {u['status']} | {last} |")
    lines.append("\nRole/team/status changes and password resets: `python manage_users.py --help` on the server.")
    await cl.Message(content="\n".join(lines)).send()

async def _decide_signup(action: cl.Action, approve: bool):
    claims = await _require_session(admin=True)
    if claims is None:
        return
    username = action.payload.get("username", "")
    role = action.payload.get("role", "user") if approve else "user"
    user, err = await asyncio.to_thread(users.decide_signup, username, approve, claims["username"], role)
    if err:
        await cl.Message(content=f"⚠️ {err}").send()
        return
    if approve:
        await cl.Message(content=f"✅ `{username}` approved as **{user['role']}** "
                                 f"(team `{user['team']}`) by `{claims['username']}`.").send()
        subject = "[NetworkGPT] Your account has been approved"
        text = (f"Hello {username},\n\nYour NetworkGPT account was approved by {claims['username']}.\n"
                f"Role: {user['role']}\nTeam: {user['team']}\n\nYou can log in now.")
    else:
        await cl.Message(content=f"❌ Signup of `{username}` rejected by `{claims['username']}`.").send()
        subject = "[NetworkGPT] Your account request was rejected"
        text = (f"Hello {username},\n\nYour NetworkGPT account request was rejected by "
                f"{claims['username']}.\nPlease contact the network admin team if you have questions.")
    if user.get("email"):
        await send_email(user["email"], subject, text)

@cl.action_callback("approve_signup")
async def on_approve_signup(action: cl.Action):
    await _decide_signup(action, approve=True)

@cl.action_callback("reject_signup")
async def on_reject_signup(action: cl.Action):
    await _decide_signup(action, approve=False)

# ============================================================
# define_epg: admin decisions at approval (tenant / AP / VRF / subnet / domains)
# ============================================================

_WIZARD_TIMEOUT = 600
_MAX_BUTTONS = 40


async def _ask_choice(prompt, options):
    """Buttons; options are values or (value, label). None = cancelled."""
    actions = []
    for o in options:
        value, label = o if isinstance(o, tuple) else (o, o)
        actions.append(cl.Action(name="wizard_pick", payload={"v": value}, label=label))
    actions.append(cl.Action(name="wizard_pick", payload={"v": "__cancel__"}, label="❌ Cancel"))
    res = await cl.AskActionMessage(content=prompt, actions=actions, timeout=_WIZARD_TIMEOUT).send()
    value = (res or {}).get("payload", {}).get("v")
    return None if value in (None, "__cancel__") else value


async def _ask_text(prompt):
    res = await cl.AskUserMessage(content=prompt, timeout=_WIZARD_TIMEOUT).send()
    return None if res is None else (res.get("output") or "").strip()


async def _ask_from_list(prompt, values):
    """Buttons for short lists, typed name (validated) for long ones."""
    if len(values) <= _MAX_BUTTONS:
        return await _ask_choice(prompt, values)
    while True:
        text = await _ask_text(f"{prompt}\n\n{len(values)} options — type the exact name "
                               "(or `cancel`):")
        if text is None or text.lower() == "cancel":
            return None
        if text in values:
            return text
        close = [v for v in values if text.lower() in v.lower()][:15]
        await cl.Message(content=f"`{text}` not found." + (f" Did you mean: {', '.join(close)}"
                                                         if close else "")).send()


async def _collect_epg_admin_input(plan, fabric) -> bool:
    """Ask the admin for the define_epg decisions and store them in
    plan["epg_admin"]. False when the admin cancels."""
    def names():
        return ", ".join(f"`{e['name']}`" for r in plan["epg_requests"] for e in r["epgs"])

    await cl.Message(content=f"**New EPG(s) need your decisions:** {names()}").send()
    try:
        current = plan["epg_requests"][0].get("pool") or "none"
        choice = await _ask_choice(
            f"0/5 — **VLAN pool**: `{current}`. VLANs are free on every VLAN API environment"
            + (f" inside `{current}`." if current != "none" else " (no pool filter)."),
            [("__keep__", "✅ Keep")] + [(p, p) for p in await list_pools() if p != current]
            + ([("none", "No pool")] if current != "none" else []))
        if choice is None:
            return False
        if choice != "__keep__":
            for req in plan["epg_requests"]:
                await change_pool(req, choice)
            await cl.Message(content=f"New VLANs from `{choice}`: {names()}").send()

        epgs = [e for r in plan["epg_requests"] for e in r["epgs"]]
        tenant = await _ask_from_list("1/5 — **Tenant**:", await epg_admin_choices(fabric, "tenants"))
        if tenant is None:
            return False
        aps = await epg_admin_choices(fabric, "aps", tenant)
        if not aps:
            await cl.Message(content=f"Tenant `{tenant}` has no application profile.").send()
            return False
        ap = await _ask_from_list(f"2/5 — **Application profile** in `{tenant}`:", aps)
        if ap is None:
            return False
        vrf = await _ask_from_list(f"3/5 — **VRF** for the new BD(s):",
                                   await epg_admin_choices(fabric, "vrfs", tenant))
        if vrf is None:
            return False

        subnets = {}
        for e in epgs:
            while True:
                text = await _ask_text(
                    f"4/5 — **Subnet** for `{e['name']}`: gateway/mask (e.g. 10.1.1.1/24), "
                    "or `none` for an L2-only BD (unicast routing off).")
                if text is None:
                    return False
                if text.lower() in ("none", "yok", "no", "-"):
                    break
                try:
                    ip = validate_subnet(text)
                except ValueError as ex:
                    await cl.Message(content=f"⚠️ {ex}").send()
                    continue
                scope = await _ask_choice(f"Scope of `{ip}`:", list(SUBNET_SCOPES))
                if scope is None:
                    return False
                subnets[e["base"]] = {"ip": ip, "scope": scope}
                break

        domains = {d["dn"]: d for r in plan["epg_requests"] for d in r.get("domains", [])}
        all_domains = None
        while True:
            current = ", ".join(f"`{d['name']}` ({d['kind']})" for d in domains.values()) or "(none)"
            choice = await _ask_choice(
                f"5/5 — **Domains**: {current}",
                [("done", "✅ Done"), ("add", "➕ Add domain")]
                + [(f"rm:{dn}", f"➖ {d['name']}") for dn, d in domains.items()])
            if choice is None:
                return False
            if choice == "done":
                break
            if choice.startswith("rm:"):
                domains.pop(choice[3:], None)
                continue
            all_domains = all_domains or await epg_admin_choices(fabric, "domains")
            options = {f"{d['name']} ({d['kind']})": d for d in all_domains if d["dn"] not in domains}
            picked = await _ask_from_list("Domain to add:", list(options))
            if picked:
                domains[options[picked]["dn"]] = options[picked]
    except VlanApiError as ex:
        await cl.Message(content=f"⚠️ VLAN API: {ex}").send()
        return False
    except Exception as ex:
        await cl.Message(content=f"⚠️ Could not read choices from APIC: {ex}").send()
        return False

    vrf_name = vrf.split("/", 1)[1] if vrf.startswith("common/") else vrf
    pool = plan["epg_requests"][0].get("pool") or "none"
    lines = [f"**Summary** — tenant `{tenant}`, AP `{ap}`, VRF `{vrf}`, VLAN pool `{pool}`"]
    for e in epgs:
        sub = subnets.get(e["base"])
        lines.append(f"- `{e['name']}` (vlan-{e['vlan']}): "
                     + (f"subnet `{sub['ip']}` {sub['scope']}, unicast routing ON" if sub
                        else "no subnet, unicast routing OFF"))
    lines.append("- Domains: " + (", ".join(d["name"] for d in domains.values()) or "(none)"))
    lines.append("\nVLANs are re-checked and marked in use on the VLAN API when applied.")
    if await _ask_choice("\n".join(lines), [("apply", "✅ Apply")]) != "apply":
        return False
    plan["epg_admin"] = {"tenant": tenant, "ap": ap, "vrf": vrf_name, "subnets": subnets,
                         "domains": list(domains.values())}
    return True
