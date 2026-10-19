"""
SMPT mailer file. Use this for SMTP client.
"""

import os
import ssl
import smtplib
import logging
from email.message import EmailMessage

def _send_sync(to_addrs, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host:
        logging.warning()
        return False
    
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]
    to_addrs= [a for a in to_addrs if a]
    
    if not to_addrs:
        logging.warning("NO recipients! skipping %s", subject)
        return False
    
    port = int(os.environ.get("SMPT_PORT", "25"))
    security = (os.environ.get("SMPT_SECURITY", "none") or "none").lower()
    user = os.environ.get("SMTP_USER") or None
    password = os.environ.get("SMTP_PASSWORD") or None
    sender = os.environ.get("SMTP_FROM") or "networkgpt@ziraatteknoloji.com"
    timeout = int(os.environ.get("SMTP_TIMEOUT", "10"))
    
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.set_content(body)
    
    try:
        if security == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx) as s:
                if user and password:
                    s.login(user, password)
                s.send_message(msg)
        
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as s:
                s.ehlo()
                if security == "starttls":
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                if user and password:
                    s.login(user, password)
                s.send_message(msg)
        
        logging.info("Email sent to %s: %s", to_addrs, subject)
        return True
    except Exception as e:
        logging.warning("Email send failed (%s): %s", subject, e)
        return False

async def send_email(to_addrs, subject, body):
    import asyncio
    return await asyncio.to_thread(_send_sync, to_addrs, subject, body)