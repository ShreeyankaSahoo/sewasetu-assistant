
import sys
import requests

sys.path.insert(0, "/workspace/app")
import app as portal
import oauth

citizen_token, _ = oauth.mint_access_token(
    subject="citizen:0000000000",
    role="citizen",
    scope="citizen",
    audience="https://sewasetu-officer-mcp-f22a.onrender.com",
    client_id="isolation-test",
)

officer_token, _ = oauth.mint_access_token(
    subject="officer:local-isolation-test",
    role="officer",
    scope="officer",
    audience="https://sewasetu-citizen-mcp-l9h2.onrender.com",
    client_id="isolation-test",
)

tests = [
    (
        "Citizen token on officer endpoint",
        "https://sewasetu-officer-mcp-f22a.onrender.com/mcp",
        citizen_token,
    ),
    (
        "Officer token on citizen endpoint",
        "https://sewasetu-citizen-mcp-l9h2.onrender.com/mcp",
        officer_token,
    ),
]

failed = False

for name, endpoint, token in tests:
    try:
        response = requests.get(
            endpoint,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        passed = response.status_code == 401
        print(
            f"{name}: HTTP {response.status_code} — "
            f"{'PASS' if passed else 'FAIL'}"
        )
        if not passed:
            print(response.text[:300])
            failed = True
    except requests.RequestException as exc:
        print(f"{name}: REQUEST ERROR — {exc}")
        failed = True

print("Role isolation:", "PASS" if not failed else "FAIL")
if failed:
    raise SystemExit(1)
