"""
gmailapi.py -- send email through the Gmail API (HTTPS) instead of SMTP.

Hosts such as Render's free plan block the email ports (25 / 465 / 587) outright, but not
HTTPS -- so the message the app already builds is handed to Gmail's users.messages.send
endpoint instead of an SMTP server. Only the delivery changes: the From name, subject, body,
footer and headers are exactly what outreach.build_email() produced.

What it needs (see the README, "Sending through the Gmail API"):
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET   the OAuth client from Google Cloud Console (server env only)
  GMAIL_REFRESH_TOKEN                       from the one-time sign-in: python3 tools/gmail_auth.py ...
                                            (server env for the admin, or Settings > Connections per account)
Only the gmail.send scope is used: the app can send as you, and can't read or delete your mail.
Access tokens last about an hour; a fresh one is fetched automatically whenever it's needed.
"""
import base64
import hashlib
import threading

import requests

import config

SCOPE = "https://www.googleapis.com/auth/gmail.send"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

REDO_SIGN_IN = ("Redo the one-time Gmail sign-in: on your computer run  python3 tools/gmail_auth.py <your client "
                "secret .json>  then paste the new refresh token into GMAIL_REFRESH_TOKEN on Render (or under "
                "Settings > Connections > Email sending) and save.")


class GmailError(RuntimeError):
    """Sending through Gmail failed. recipient=True: Gmail turned down this address (the lead's
    problem). auth=True: the stored sign-in no longer works and has to be redone."""

    def __init__(self, message, recipient=False, auth=False):
        super().__init__(message)
        self.recipient = recipient
        self.auth = auth


def _settings():
    return (config.env("GOOGLE_CLIENT_ID").strip(), config.env("GOOGLE_CLIENT_SECRET").strip(),
            config.env("GMAIL_REFRESH_TOKEN").strip())


def configured():
    """True when this account sends through the Gmail API rather than SMTP."""
    return all(_settings())


# One set of credentials (and its current access token) per account, kept between sends so
# the token is only refreshed when it has actually run out.
_creds = {}
_lock = threading.Lock()


def _credentials(force_refresh=False):
    from google.auth.exceptions import RefreshError, TransportError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    client_id, client_secret, refresh_token = _settings()
    if not (client_id and client_secret and refresh_token):
        raise GmailError("The Gmail API isn't set up: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GMAIL_REFRESH_TOKEN "
                         "are all needed.", auth=True)
    key = hashlib.sha256(f"{client_id}|{refresh_token}".encode()).hexdigest()
    with _lock:
        creds = _creds.get(key)
        if creds is None:
            creds = Credentials(token=None, refresh_token=refresh_token, client_id=client_id,
                                client_secret=client_secret, token_uri=TOKEN_URI, scopes=[SCOPE])
            _creds[key] = creds
        if force_refresh or not creds.valid:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                _creds.pop(key, None)
                text = str(e)
                if "invalid_grant" in text or "expired or revoked" in text.lower():
                    raise GmailError(f"Gmail no longer accepts the saved sign-in (Google said: {text[:160]}). "
                                     f"{REDO_SIGN_IN}", auth=True)
                if "invalid_client" in text or "unauthorized_client" in text:
                    raise GmailError(f"Google rejected the OAuth client (Google said: {text[:160]}). Check that "
                                     "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET on Render match the client the refresh "
                                     f"token was made with. {REDO_SIGN_IN}", auth=True)
                raise GmailError(f"Couldn't get a Gmail access token (Google said: {text[:200]}).")
            except TransportError as e:
                raise GmailError(f"Couldn't reach Google to refresh the Gmail sign-in ({str(e)[:160]}).")
        return creds


def _error_text(resp):
    try:
        err = resp.json().get("error", {})
        reasons = ",".join(x.get("reason", "") for x in err.get("errors", []) if isinstance(x, dict))
        return (err.get("message") or resp.text[:200]), reasons
    except ValueError:
        return resp.text[:200], ""


def send(msg, thread_id=None):
    """Send an email.message.EmailMessage as it is. Returns Gmail's {"id", "threadId"}.
    Raises GmailError (see its flags) when it can't."""
    body = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")}
    if thread_id:
        body["threadId"] = thread_id          # keeps a follow-up in the same conversation in Gmail
    for attempt in (1, 2):
        creds = _credentials(force_refresh=attempt == 2)
        try:
            resp = requests.post(SEND_URL, json=body, timeout=30,
                                 headers={"Authorization": f"Bearer {creds.token}"})
        except requests.RequestException as e:
            raise GmailError(f"Couldn't reach the Gmail API ({str(e)[:160]}).")
        if resp.status_code == 401 and attempt == 1:
            continue                          # the access token was revoked early: get a new one, try once more
        if resp.ok:
            data = resp.json()
            return {"id": data.get("id", ""), "threadId": data.get("threadId", "")}
        text, reasons = _error_text(resp)
        if resp.status_code == 401:
            raise GmailError(f"Gmail refused the sign-in ({text}). {REDO_SIGN_IN}", auth=True)
        if resp.status_code == 403 and ("insufficient" in text.lower() or "scope" in text.lower()):
            raise GmailError(f"The saved Gmail sign-in isn't allowed to send email ({text}). {REDO_SIGN_IN}", auth=True)
        if resp.status_code == 403 and ("has not been used" in text or "disabled" in text or "accessNotConfigured" in reasons):
            raise GmailError("The Gmail API is switched off for this Google Cloud project. In Google Cloud Console "
                             "open APIs & Services > Library, find Gmail API and press Enable, then try again.")
        if resp.status_code == 429 or "RateLimitExceeded" in reasons or "dailyLimitExceeded" in reasons \
                or "Limit Exceeded" in text:
            raise GmailError(f"Gmail's sending limit for this account has been reached ({text}). Try again later.")
        if resp.status_code == 400 and ("to header" in text.lower() or "recipient" in text.lower()
                                        or "invalid to" in text.lower()):
            raise GmailError(f"Gmail refused this address ({text}).", recipient=True)
        raise GmailError(f"Gmail didn't accept the message (HTTP {resp.status_code}: {text}).")
    raise GmailError(f"Gmail refused the sign-in. {REDO_SIGN_IN}", auth=True)


def test():
    """Check the stored sign-in works -- gets a fresh access token, sends nothing.
    (The gmail.send permission can't read the mailbox, so this is as far as a test can go.)"""
    _credentials(force_refresh=True)
    return {"ok": True, "message": "The Gmail API accepted your sign-in and handed out an access token. "
                                   "Email will be sent through it (nothing was sent just now)."}


# ------------------------------------------------------------ "Connect Gmail" (in the browser)
# The same sign-in as tools/gmail_auth.py, done on the website: Google sends the person back to
# <PUBLIC_URL>/oauth/google/callback with a one-time code, which is swapped for a refresh token.
# The OAuth client must list that address under "Authorized redirect URIs".
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
WEB_SCOPES = f"openid email {SCOPE}"


def web_client_ready():
    return bool(config._server_env("GOOGLE_CLIENT_ID") and config._server_env("GOOGLE_CLIENT_SECRET"))


def consent_url(redirect_uri, state, login_hint=""):
    from urllib.parse import urlencode
    q = {"client_id": config._server_env("GOOGLE_CLIENT_ID"), "redirect_uri": redirect_uri, "response_type": "code",
         "scope": WEB_SCOPES, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
         "state": state}
    if login_hint:
        q["login_hint"] = login_hint
    return f"{AUTH_URI}?{urlencode(q)}"


def exchange_code(code, redirect_uri):
    """One-time code -> (refresh token, the Google address that signed in)."""
    try:
        r = requests.post(TOKEN_URI, data={
            "code": code, "client_id": config._server_env("GOOGLE_CLIENT_ID"),
            "client_secret": config._server_env("GOOGLE_CLIENT_SECRET"), "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"}, timeout=20)
    except requests.RequestException as e:
        raise GmailError(f"Couldn't reach Google ({e}).")
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200:
        why = data.get("error_description") or data.get("error") or r.text[:200]
        if "redirect_uri_mismatch" in str(data.get("error", "")):
            why = (f"Google doesn't know this site's address yet. In Google Cloud Console > Credentials > your "
                   f"OAuth client, add {redirect_uri} under Authorized redirect URIs, save, wait a few minutes "
                   "and try again.")
        raise GmailError(f"Google refused the sign-in: {why}")
    refresh = data.get("refresh_token")
    if not refresh:
        raise GmailError("Google didn't hand out a long-lived sign-in. Remove Nexus Reach under myaccount.google.com > "
                         "Security > Third-party access, then connect again.")
    granted = set((data.get("scope") or "").split())
    if SCOPE not in granted:
        raise GmailError("The 'send email on your behalf' permission wasn't ticked. Connect again and allow it.")
    email = ""
    idt = data.get("id_token") or ""
    if idt.count(".") == 2:
        import json as _json
        part = idt.split(".")[1]
        try:
            email = _json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email", "")
        except (ValueError, TypeError):
            email = ""
    return refresh, email
