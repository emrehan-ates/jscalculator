"""
Self-service signup page served by Chainlit's own FastAPI app.

GET  /signup  -> form (username, e-mail, team, password)
POST /signup  -> stores a PENDING user and e-mails the admins

An admin then types `signups` in the chat to approve or reject. Chainlit's
frontend has a catch-all route, so register() moves these routes in front of
it. public/signup-link.js adds a "Sign up" link to the login page (enable it
with custom_js in .chainlit/config.toml).
"""
import html
import logging
import time
from collections import defaultdict, deque

from fastapi import Form, Request
from fastapi.responses import HTMLResponse

import users
from mailer import send_email

PATH = "/signup"
_RATE_WINDOW = 3600     # seconds
_RATE_MAX = 5           # signup attempts per client IP per window
_attempts = defaultdict(deque)

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NetworkGPT — Sign up</title>
<style>
  :root {{ --bg:#f5f6f8; --card:#fff; --fg:#1d2433; --muted:#5b6475; --line:#d6dae1;
          --accent:#2f6fde; --err:#b42318; --ok:#17803d; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#14171c; --card:#1d2128; --fg:#e8ebf0; --muted:#9aa3b2; --line:#323844;
            --accent:#6a9cf5; --err:#f97066; --ok:#47cd89; }} }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg);
         color:var(--fg); font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; padding:16px }}
  .card {{ width:100%; max-width:400px; background:var(--card); border:1px solid var(--line);
          border-radius:12px; padding:28px }}
  h1 {{ font-size:20px; margin:0 0 4px }}
  p.sub {{ color:var(--muted); margin:0 0 20px }}
  label {{ display:block; font-weight:600; margin:14px 0 6px }}
  input {{ width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:8px;
          background:transparent; color:var(--fg); font:inherit }}
  button {{ width:100%; margin-top:22px; padding:11px; border:0; border-radius:8px;
           background:var(--accent); color:#fff; font:inherit; font-weight:600; cursor:pointer }}
  .msg {{ padding:10px 12px; border-radius:8px; margin-bottom:12px; border:1px solid }}
  .err {{ color:var(--err); border-color:var(--err) }}
  .ok {{ color:var(--ok); border-color:var(--ok) }}
  .foot {{ margin-top:18px; text-align:center; color:var(--muted) }}
  a {{ color:var(--accent) }}
</style></head><body><main class="card">
<h1>Request access</h1>
<p class="sub">An administrator will review your request.</p>
{message}
{form}
<p class="foot"><a href="/login">Back to login</a></p>
</main></body></html>"""

_FORM = """<form method="post" action="{path}" autocomplete="off">
  <label for="u">Username</label>
  <input id="u" name="username" required minlength="3" maxlength="32"
         pattern="[A-Za-z0-9._\\-]+" value="{username}">
  <label for="e">E-mail</label>
  <input id="e" name="email" type="email" required value="{email}">
  <label for="t">Team</label>
  <input id="t" name="team" required maxlength="16" list="teams" placeholder="MSY, CSY…" value="{team}">
  <datalist id="teams"><option value="MSY"><option value="CSY"><option value="SG"></datalist>
  <label for="p">Password</label>
  <input id="p" name="password" type="password" required minlength="{minlen}" autocomplete="new-password">
  <label for="p2">Repeat password</label>
  <input id="p2" name="password2" type="password" required minlength="{minlen}" autocomplete="new-password">
  <button type="submit">Send request</button>
</form>"""


def _render(message="", kind="err", values=None, show_form=True):
    v = {k: html.escape((values or {}).get(k, "")) for k in ("username", "email", "team")}
    msg = f'<div class="msg {kind}">{html.escape(message)}</div>' if message else ""
    form = _FORM.format(path=PATH, minlen=users.MIN_PASSWORD_LEN, **v) if show_form else ""
    return HTMLResponse(_PAGE.format(message=msg, form=form),
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


def _rate_limited(ip):
    now = time.monotonic()
    q = _attempts[ip]
    while q and now - q[0] > _RATE_WINDOW:
        q.popleft()
    if len(q) >= _RATE_MAX:
        return True
    q.append(now)
    return False


async def signup_form():
    return _render()


async def signup_submit(request: Request, username: str = Form(""), email: str = Form(""),
                        team: str = Form(""), password: str = Form(""), password2: str = Form("")):
    values = {"username": username, "email": email, "team": team}
    if _rate_limited(request.client.host if request.client else "?"):
        return _render("Too many signup attempts. Please try again later.", values=values)
    if password != password2:
        return _render("Passwords do not match.", values=values)
    user, err = users.create_signup(username, password, email, team)
    if err:
        return _render(err, values=values)

    admins = users.get_admin_emails()
    if admins:
        try:
            await send_email(
                admins,
                f"[NetworkGPT] New signup request: {user['username']}",
                (f"A new user asked for access.\n\n"
                 f"Username : {user['username']}\nE-mail   : {user['email']}\nTeam     : {user['team']}\n\n"
                 "Log in and type 'signups' to approve or reject."),
            )
        except Exception as e:  # mail is best effort; the request is stored
            logging.warning("signup admin mail failed: %s", e)
    return _render("Your request was sent. You can log in after an administrator approves it.",
                   kind="ok", show_form=False)


def register(app):
    """Add the signup routes to Chainlit's FastAPI app, ahead of its
    catch-all frontend route."""
    app.router.add_api_route(PATH, signup_form, methods=["GET"], response_class=HTMLResponse)
    app.router.add_api_route(PATH, signup_submit, methods=["POST"], response_class=HTMLResponse)
    routes = app.router.routes
    mine = [r for r in routes if getattr(r, "endpoint", None) in (signup_form, signup_submit)]
    for r in mine:
        routes.remove(r)
    routes[0:0] = mine
