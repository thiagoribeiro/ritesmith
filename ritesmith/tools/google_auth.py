"""One-time OAuth2 flow to obtain and store a Google token.json.

Usage:
    python -m ritesmith.tools.google_auth

Requires:
    RITESMITH_GOOGLE_CLIENT_SECRETS_JSON = path to OAuth client secrets JSON
    RITESMITH_GOOGLE_TOKEN_JSON          = where to write the resulting token

The resulting token covers both Calendar and Gmail scopes.
"""
import os
import sys

_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
]


def main() -> None:
    from ritesmith.config import get_settings
    s = get_settings()

    secrets = s.google_client_secrets_json
    token_out = s.google_token_json

    if not secrets:
        print("ERROR: RITESMITH_GOOGLE_CLIENT_SECRETS_JSON is not set.", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(secrets):
        print(f"ERROR: Client secrets file not found: {secrets}", file=sys.stderr)
        sys.exit(1)
    if not token_out:
        print("ERROR: RITESMITH_GOOGLE_TOKEN_JSON is not set.", file=sys.stderr)
        sys.exit(1)

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(secrets, _SCOPES)
    creds = flow.run_local_server(port=0)

    os.makedirs(os.path.dirname(os.path.abspath(token_out)), exist_ok=True)
    with open(token_out, "w") as f:
        f.write(creds.to_json())

    print(f"Token saved to: {token_out}")
    print("Scopes granted:")
    for scope in _SCOPES:
        print(f"  {scope}")


if __name__ == "__main__":
    main()
