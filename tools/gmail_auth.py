#!/usr/bin/env python3
"""
One-time Gmail sign-in, run on YOUR computer (not on Render). It opens Google's consent page,
you approve "Send email on your behalf", and it prints the refresh token Nexus Reach needs.

    pip3 install google-auth-oauthlib
    python3 tools/gmail_auth.py ~/Downloads/client_secret_XXXX.apps.googleusercontent.com.json

Optional:  --email you@gmail.com   (pre-fills the account on Google's page)
           --port 8080             (must match the redirect URI registered for a "Web application" client)

Then put these on Render (Environment tab) -- never in the repo:
    GOOGLE_CLIENT_ID      (printed below)
    GOOGLE_CLIENT_SECRET  (printed below)
    GMAIL_REFRESH_TOKEN   (printed below)
Only the gmail.send permission is requested: the app can send as you and nothing else.
"""
import argparse
import json
import sys

SCOPE = "https://www.googleapis.com/auth/gmail.send"


def main():
    ap = argparse.ArgumentParser(description="Get a Gmail API refresh token for Nexus Reach.")
    ap.add_argument("client_secret_json", help="the OAuth client file downloaded from Google Cloud Console")
    ap.add_argument("--email", default="", help="the Gmail address to sign in with (optional)")
    ap.add_argument("--port", type=int, default=8080, help="local port for Google to send you back to (default 8080)")
    args = ap.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("First run:  pip3 install google-auth-oauthlib")

    with open(args.client_secret_json) as f:
        info = json.load(f)
    kind = "web" if "web" in info else "installed" if "installed" in info else None
    if not kind:
        sys.exit("That file doesn't look like an OAuth client file from Google Cloud Console.")
    client = info[kind]
    if kind == "web":
        print(f"\nThis is a 'Web application' client. Before continuing, make sure Google Cloud Console >\n"
              f"APIs & Services > Credentials > (this client) > Authorized redirect URIs contains exactly:\n"
              f"    http://localhost:{args.port}/\n"
              f"(or create a 'Desktop app' client instead, which needs no redirect URI).\n")

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secret_json, scopes=[SCOPE])
    extra = {"login_hint": args.email} if args.email else {}
    creds = flow.run_local_server(port=args.port, access_type="offline", prompt="consent", open_browser=True,
                                  authorization_prompt_message="Opening Google's sign-in page in your browser...\n"
                                                               "If it doesn't open, visit: {url}\n",
                                  success_message="Done -- you can close this tab and go back to the terminal.",
                                  **extra)
    if not creds.refresh_token:
        sys.exit("Google didn't hand out a refresh token. Remove the app's access at "
                 "myaccount.google.com/permissions, then run this again.")
    print("\nSigned in. Add these three to Render > your service > Environment (then Save and deploy):\n")
    print(f"GOOGLE_CLIENT_ID={client['client_id']}")
    print(f"GOOGLE_CLIENT_SECRET={client['client_secret']}")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
    print("\nKeep them out of the repo and out of chat. If the OAuth consent screen is still in 'Testing',\n"
          "Google expires this token after 7 days -- set it to 'In production' to keep it.")


if __name__ == "__main__":
    main()
