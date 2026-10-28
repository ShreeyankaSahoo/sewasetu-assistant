
import asyncio
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app")

# Import the Flask app first because the existing app.py and oauth.py
# initialize each other.
import app as portal
import oauth

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


CITIZEN_ENDPOINT = "http://citizen-mcp:8001/mcp"
OFFICER_ENDPOINT = "http://officer-mcp:8002/mcp"

CITIZEN_AUDIENCE = "http://localhost:8001"
OFFICER_AUDIENCE = "http://localhost:8002"

RESULT_PATH = Path("/evals/results.json")

# A deliberately synthetic document fixture for local workflow testing.
# It is NOT a genuine age proof and must never be used for a real applicant.
SYNTHETIC_PDF = base64.b64encode(
    b"%PDF-1.4\n% SYNTHETIC EVALUATION FIXTURE - NOT REAL AGE PROOF\n"
    b"1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
).decode("ascii")


def new_case(case_id, persona, scenario):
    return {
        "id": case_id,
        "persona": persona,
        "scenario": scenario,
        "transcript": [],
        "assertions": [],
        "passed": False,
    }


def record_action(case, actor, tool, result):
    case["transcript"].append({
        "actor": actor,
        "tool": tool,
        "result": result,
    })


def check(case, name, expected, actual):
    case["assertions"].append({
        "name": name,
        "expected": expected,
        "actual": actual,
        "passed": expected == actual,
    })


def parse_tool_result(result):
    for item in result.content:
        if not hasattr(item, "text"):
            continue
        try:
            return json.loads(item.text)
        except (json.JSONDecodeError, TypeError):
            return {"text": item.text, "is_error": result.isError}
    return {"is_error": result.isError, "message": "No text result returned"}


async def call_tool(endpoint, token, tool_name, arguments):
    headers = {"Authorization": f"Bearer {token}"}

    async with streamablehttp_client(endpoint, headers=headers) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments=arguments)
            return parse_tool_result(result)


def database_application(mobile):
    conn = portal.get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT id, application_no, status, decided_by
            FROM applications
            WHERE mobile = %s
            ORDER BY submitted_at DESC
            LIMIT 1
            """,
            (mobile,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "application_number": row[1],
            "status": row[2],
            "decided_by": row[3],
        }
    finally:
        cur.close()
        conn.close()


def database_audit(application_id, action, actor, note):
    conn = portal.get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT 1
            FROM audit_log
            WHERE application_id = %s
              AND action = %s
              AND actor = %s
              AND note = %s
            LIMIT 1
            """,
            (application_id, action, actor, note),
        )
        return cur.fetchone() is not None
    finally:
        cur.close()
        conn.close()


def make_token(subject, role, scope, audience, client_id):
    token, _ = oauth.mint_access_token(
        subject=subject,
        role=role,
        scope=scope,
        audience=audience,
        client_id=client_id,
    )
    return token


async def run_valid_applicant(citizen_token, case, mobile, applicant):
    eligibility = await call_tool(
        CITIZEN_ENDPOINT,
        citizen_token,
        "check_eligibility",
        {
            "date_of_birth": applicant["dob"],
            "village": applicant["village"],
            "block": applicant["block"],
        },
    )
    record_action(case, "citizen", "check_eligibility", eligibility)
    check(
        case,
        "Preliminary eligibility passed",
        True,
        eligibility.get("preliminary_checks_passed"),
    )

    upload = await call_tool(
        CITIZEN_ENDPOINT,
        citizen_token,
        "upload_age_proof",
        {
            "filename": "synthetic-age-proof.pdf",
            "content_base64": SYNTHETIC_PDF,
        },
    )
    record_action(case, "citizen", "upload_age_proof", upload)
    check(case, "Synthetic fixture upload succeeded", True, upload.get("success"))

    if not upload.get("success"):
        check(case, "Application submitted", True, False)
        case["passed"] = False
        return None

    submission = await call_tool(
        CITIZEN_ENDPOINT,
        citizen_token,
        "submit_application",
        {
            "applicant_name": applicant["name"],
            "date_of_birth": applicant["dob"],
            "village": applicant["village"],
            "block": applicant["block"],
            "bank_account": applicant["bank_account"],
            "ifsc": "SBIN0003077",
            "gender": "Female",
            "marital_status": "Widowed",
            "document_id": upload["document_id"],
        },
    )
    record_action(case, "citizen", "submit_application", submission)
    check(case, "Submission succeeded", True, submission.get("submitted"))

    record = database_application(mobile)
    check(case, "Application number returned", True, bool(submission.get("application_number")))
    check(case, "Application exists in database", True, record is not None)

    if record:
        check(case, "Database application number matches", submission.get("application_number"), record["application_number"])
    return record


async def main():
    results = {
        "title": "Sewa Setu MCP Evaluation Results",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": "Local Docker development environment",
        "evaluation_method": (
            "Scripted tool-level tests using the Python MCP client and direct "
            "PostgreSQL assertions. These are not transcripts from a free-form LLM conversation."
        ),
        "document_fixture_note": (
            "The uploaded PDF is synthetic test data, not a genuine age proof. "
            "Portal upload currently checks filename extension and size; this test "
            "does not validate document authenticity."
        ),
        "personas": [
            {
                "name": "Mina Das",
                "age": 76,
                "village": "Sonari Basti",
                "block": "Sonari",
                "voice": "Polite and direct; wants help applying for the pension.",
                "goal": "Submit an eligible application successfully.",
                "expected_database_state": "One PENDING application for the synthetic test mobile.",
            },
            {
                "name": "Ranjit Bora",
                "age": 51,
                "village": "Rajapara",
                "block": "Rajapara",
                "voice": "Impatient; wants to submit despite not meeting the age threshold.",
                "goal": "Ensure the portal rejects an underage application and invalid bank number.",
                "expected_database_state": "No application for the synthetic test mobile.",
            },
            {
                "name": "Kamala Saikia",
                "age": 68,
                "village": "Namti Gaon",
                "block": "Namti",
                "voice": "Careful and respectful; wants to apply and receive a clear outcome.",
                "goal": "Submit successfully, then verify an officer decision and audit reason.",
                "expected_database_state": "One REJECTED application with officer identity and reason recorded.",
            },
        ],
        "evaluations": [],
    }

    mobiles = {
        "mina": "0000000101",
        "ranjit": "0000000102",
        "kamala": "0000000103",
    }

    # Refuse to overwrite or duplicate existing test records.
    for mobile in mobiles.values():
        existing = database_application(mobile)
        if existing is not None:
            raise RuntimeError(
                f"Test mobile {mobile} already has an application. "
                "Choose a fresh synthetic mobile before rerunning evaluations."
            )

    citizen_tokens = {
        key: make_token(
            f"citizen:{mobile}",
            "citizen",
            "citizen",
            CITIZEN_AUDIENCE,
            f"eval-citizen-{key}",
        )
        for key, mobile in mobiles.items()
    }

    officer_token = make_token(
        "officer:local-evaluation-officer",
        "officer",
        "officer",
        OFFICER_AUDIENCE,
        "eval-officer",
    )

    # Evaluation 1: eligible application should submit and remain pending.
    case = new_case(
        "CIT-001",
        "Mina Das, 76, Sonari Basti",
        "Eligible applicant submits an old-age pension application.",
    )
    record = await run_valid_applicant(
        citizen_tokens["mina"],
        case,
        mobiles["mina"],
        {
            "name": "Mina Das",
            "dob": "15/06/1950",
            "village": "Sonari Basti",
            "block": "Sonari",
            "bank_account": "123456789012",
        },
    )
    check(case, "Database status is PENDING", "PENDING", record["status"] if record else None)
    case["passed"] = all(a["passed"] for a in case["assertions"])
    results["evaluations"].append(case)

    # Evaluation 2: underage applicant and invalid bank account must be rejected.
    case = new_case(
        "CIT-002",
        "Ranjit Bora, 51, Rajapara",
        "Underage applicant supplies an invalid bank account.",
    )
    eligibility = await call_tool(
        CITIZEN_ENDPOINT,
        citizen_tokens["ranjit"],
        "check_eligibility",
        {
            "date_of_birth": "01/01/1975",
            "village": "Rajapara",
            "block": "Rajapara",
        },
    )
    record_action(case, "citizen", "check_eligibility", eligibility)
    check(case, "Underage preliminary eligibility fails", False, eligibility.get("preliminary_checks_passed"))

    upload = await call_tool(
        CITIZEN_ENDPOINT,
        citizen_tokens["ranjit"],
        "upload_age_proof",
        {
            "filename": "synthetic-age-proof.pdf",
            "content_base64": SYNTHETIC_PDF,
        },
    )
    record_action(case, "citizen", "upload_age_proof", upload)
    check(case, "Synthetic fixture upload succeeded", True, upload.get("success"))

    submission = await call_tool(
        CITIZEN_ENDPOINT,
        citizen_tokens["ranjit"],
        "submit_application",
        {
            "applicant_name": "Ranjit Bora",
            "date_of_birth": "01/01/1975",
            "village": "Rajapara",
            "block": "Rajapara",
            "bank_account": "123",
            "ifsc": "",
            "gender": "Male",
            "marital_status": "Married",
            "document_id": upload.get("document_id", ""),
        },
    ) if upload.get("success") else {"submitted": False, "validation_errors": []}

    record_action(case, "citizen", "submit_application", submission)
    check(case, "Submission was blocked", False, submission.get("submitted"))
    errors = submission.get("validation_errors", [])
    check(case, "Age validation error returned", True, any("60 years" in e for e in errors))
    check(case, "Bank-number validation error returned", True, any("bank account number" in e for e in errors))
    check(case, "No application was created", None, database_application(mobiles["ranjit"]))
    case["passed"] = all(a["passed"] for a in case["assertions"])
    results["evaluations"].append(case)

    # Evaluation 3: officer reviews the application and records a rejection.
    case = new_case(
        "OFF-001",
        "Kamala Saikia, 68, Namti Gaon",
        "Officer inspects an application and rejects it with an explicit reason.",
    )
    record = await run_valid_applicant(
        citizen_tokens["kamala"],
        case,
        mobiles["kamala"],
        {
            "name": "Kamala Saikia",
            "dob": "05/05/1958",
            "village": "Namti Gaon",
            "block": "Namti",
            "bank_account": "123456789013",
        },
    )

    if record:
        listing = await call_tool(
            OFFICER_ENDPOINT,
            officer_token,
            "list_pending_applications",
            {"block": "Namti", "limit": 100},
        )
        record_action(case, "officer", "list_pending_applications", listing)
        listed = [
            item.get("application_number")
            for item in listing.get("applications", [])
        ]
        check(
            case,
            "Officer can see the pending test application",
            record["application_number"] in listed,
            True if record["application_number"] in listed else False,
        )

        inspected = await call_tool(
            OFFICER_ENDPOINT,
            officer_token,
            "inspect_application",
            {"application_number": record["application_number"]},
        )
        record_action(case, "officer", "inspect_application", inspected)
        check(case, "Officer can inspect application", True, inspected.get("found"))

        reason = "Synthetic evaluation case: rejection workflow verification."
        decision = await call_tool(
            OFFICER_ENDPOINT,
            officer_token,
            "decide_application",
            {
                "application_number": record["application_number"],
                "decision": "REJECTED",
                "reason": reason,
            },
        )
        record_action(case, "officer", "decide_application", decision)
        check(case, "Officer decision tool succeeded", True, decision.get("success"))

        after = database_application(mobiles["kamala"])
        check(case, "Database status is REJECTED", "REJECTED", after["status"] if after else None)
        check(case, "Officer identity stored in database", "local-evaluation-officer", after["decided_by"] if after else None)

        audit_ok = database_audit(
            after["id"] if after else -1,
            "REJECTED",
            "admin:local-evaluation-officer",
            reason,
        )
        check(case, "Rejection reason appears in audit log", True, audit_ok)
    else:
        check(case, "Officer decision tool succeeded", True, False)
        check(case, "Database status is REJECTED", "REJECTED", None)
        check(case, "Rejection reason appears in audit log", True, False)

    case["passed"] = all(a["passed"] for a in case["assertions"])
    results["evaluations"].append(case)

    total_assertions = sum(len(c["assertions"]) for c in results["evaluations"])
    passed_assertions = sum(
        1
        for c in results["evaluations"]
        for assertion in c["assertions"]
        if assertion["passed"]
    )

    results["summary"] = {
        "total_scenarios": len(results["evaluations"]),
        "passed_scenarios": sum(1 for c in results["evaluations"] if c["passed"]),
        "failed_scenarios": sum(1 for c in results["evaluations"] if not c["passed"]),
        "total_assertions": total_assertions,
        "passed_assertions": passed_assertions,
        "pass_rate_percent": round(100 * passed_assertions / total_assertions, 2) if total_assertions else 0,
    }

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(results, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps(results["summary"], indent=2))
    print(f"Detailed results saved to {RESULT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
