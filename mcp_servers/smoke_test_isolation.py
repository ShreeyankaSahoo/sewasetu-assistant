import sys
import requests

sys.path.insert(0, "/app")
import app as portal
import oauth

citizen_token, _ = oauth.mint_access_token(
    subject="citizen:0000000000",
    role="citizen",
    scope="citizen",
    audience="http://localhost:8001",
    client_id="isolation-test",
)

officer_token, _ = oauth.mint_access_token(
    subject="officer:local-isolation-test",
    role="officer",
    scope="officer",
    audience="http://localhost:8002",
    client_id="isolation-test",
)

tests = [
    (
        "Citizen token on officer endpoint",
        "http://officer-mcp:8002/mcp",
        citizen_token,
    ),
    (
        "Officer token on citizen endpoint",
        "http://citizen-mcp:8001/mcp",
        officer_token,
    ),
]

failed = False

for name, endpoint, token in tests:
    response = requests.get(
        endpoint,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    passed = response.status_code == 401
    print(f"{name}: HTTP {response.status_code} — {'PASS' if passed else 'FAIL'}")
    if not passed:
        print(response.text[:500])
        failed = True

print("Role isolation:", "PASS" if not failed else "FAIL")
if failed:
    raise SystemExit(1)
