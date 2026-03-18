import requests
import random
import string
import re
import time

# Optional dependency; keep module usable without it.
try:
    from colorama import Fore  # noqa: F401
except Exception:
    Fore = None  # type: ignore

MAILTM_BASE = "https://api.mail.tm"


def _members(json_obj):
    # mail.tm can return Hydra dicts or plain lists depending on deployment.
    if isinstance(json_obj, list):
        return json_obj
    if isinstance(json_obj, dict):
        return json_obj.get("hydra:member") or json_obj.get("member") or []
    return []

def get_available_domains():
    url = f"{MAILTM_BASE}/domains"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    domains = _members(response.json())
    out = []
    for d in domains:
        if not isinstance(d, dict):
            continue
        dom = (d.get("domain") or d.get("name") or "").strip()
        if not dom:
            continue
        if d.get("isPrivate") is True:
            continue
        if d.get("isActive") is False:
            continue
        out.append(dom)
    return out

def generate_random_email_with_domain(domain):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=10)) + "@" + domain

def save_account_to_file(email, password):
    with open("Mail.txt", "a", encoding="utf-8") as f:
        f.write(f"{email}|{password}\n")

def create_mailtm_account(email, password):
    r = requests.post(
        f"{MAILTM_BASE}/accounts",
        json={"address": email, "password": password},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        return None
    return r.json()


def create_random_mailtm_account(domains, password):
    domain = random.choice(domains)
    email = generate_random_email_with_domain(domain)

    j = create_mailtm_account(email, password)

    if j:
        save_account_to_file(email, password)
        return j, email, password

    return None, None, None

def login_mailtm(email, password):
    r = requests.post(
        f"{MAILTM_BASE}/token",
        json={"address": email, "password": password},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("token")

def check_inbox_mailtm(token):
    r = requests.get(
        f"{MAILTM_BASE}/messages",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )

    for msg in _members(r.json()):
        if not isinstance(msg, dict) or "id" not in msg:
            continue
        code = read_email_message(token, msg["id"])
        if code:
            return code
    return None

def read_email_message(token, msg_id):
    r = requests.get(
        f"{MAILTM_BASE}/messages/{msg_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )

    j = r.json()
    blob = "\n".join(
        str(x).strip()
        for x in [
            j.get("text") or "",
            j.get("html") or "",
            j.get("intro") or "",
            j.get("subject") or "",
        ]
        if x is not None
    )
    match = re.search(r"\b\d{6}\b", blob)
    return match.group(0) if match else None


def wait_for_otp(token, *, timeout_s=180, poll_s=2.0, regex=r"\b(\d{6})\b"):
    deadline = time.time() + timeout_s
    rx = re.compile(regex)
    seen = set()

    while time.time() < deadline:
        r = requests.get(
            f"{MAILTM_BASE}/messages",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        r.raise_for_status()
        msgs = _members(r.json())

        # newest first if available
        msgs = [m for m in msgs if isinstance(m, dict)]
        msgs.sort(key=lambda m: (m.get("createdAt") or ""), reverse=True)

        for m in msgs:
            mid = str(m.get("id") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)

            r2 = requests.get(
                f"{MAILTM_BASE}/messages/{mid}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            r2.raise_for_status()
            full = r2.json()

            blob = "\n".join(
                str(x).strip()
                for x in [
                    full.get("text") or "",
                    full.get("html") or "",
                    full.get("intro") or "",
                    full.get("subject") or "",
                    m.get("intro") or "",
                    m.get("subject") or "",
                ]
                if x is not None
            )

            m2 = rx.search(blob)
            if m2:
                return m2.group(1)

        time.sleep(poll_s)

    return None
