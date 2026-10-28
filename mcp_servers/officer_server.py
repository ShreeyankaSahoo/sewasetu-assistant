
import logging
import os
import sys
from datetime import datetime

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP

# Reuse the existing portal's database and audit functions.
sys.path.insert(0, "/app")
import app as portal


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sewasetu.officer-mcp")

HOST = "0.0.0.0"
PORT = int(os.getenv("MCP_PORT", "8002"))

AUDIENCE = os.getenv(
    "MCP_AUDIENCE",
    "http://localhost:8002",
)
ISSUER = os.getenv(
    "PORTAL_ISSUER",
    "http://localhost:8000",
)
JWKS_URL = os.getenv(
    "JWKS_URL",
    "http://app:8000/.well-known/jwks.json",
)

jwks_client = PyJWKClient(JWKS_URL)


def decode_officer_token(token: str) -> dict:
    """Verify token signature, issuer, audience, role, and scope."""
    key = jwks_client.get_signing_key_from_jwt(token)

    claims = jwt.decode(
        token,
        key.key,
        algorithms=["RS256"],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": ["iss", "sub", "aud", "iat", "exp", "jti"]},
    )

    if claims.get("role") != "officer":
        raise ValueError("An officer token is required.")

    if claims.get("scope") != "officer":
        raise ValueError("The token does not have officer scope.")

    subject = claims.get("sub", "")
    if not isinstance(subject, str) or not subject.startswith("officer:"):
        raise ValueError("The token does not identify an officer.")

    if not subject.partition(":")[2]:
        raise ValueError("Officer identity is missing.")

    kind = claims.get("kind")
    if kind not in ("access", "program"):
        raise ValueError("Unsupported token type.")

    # Check admin-issued program tokens against the portal's revocation table.
    if kind == "program":
        conn = portal.get_db()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT 1
                FROM oauth_program_tokens
                WHERE jti = %s
                  AND subject = %s
                  AND audience = %s
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > %s)
                """,
                (
                    claims["jti"],
                    subject,
                    AUDIENCE,
                    datetime.now(),
                ),
            )
            if cur.fetchone() is None:
                raise ValueError("The officer program token is revoked or inactive.")
        finally:
            cur.close()
            conn.close()

    return claims


class SewaSetuOfficerTokenVerifier(TokenVerifier):
    """MCP SDK verifier that permits only valid officer tokens."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = decode_officer_token(token)
            return AccessToken(
                token=token,
                client_id=str(claims.get("client_id", "")),
                scopes=["officer"],
                expires_at=int(claims["exp"]),
                resource=AUDIENCE,
            )
        except Exception as exc:
            logger.warning(
                "Officer token verification failed: %s",
                type(exc).__name__,
            )
            return None


mcp = FastMCP(
    name="Sewa Setu Block Officer Assistant",
    instructions=(
        "Tools for authorized block officers. Inspect pending pension cases, "
        "and approve or reject only pending applications. A decision must have "
        "a meaningful reason recorded in the portal audit log. Never invent an "
        "application or claim a decision succeeded unless the database confirms it."
    ),
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
    token_verifier=SewaSetuOfficerTokenVerifier(),
    auth=AuthSettings(
        issuer_url=ISSUER,
        resource_server_url=AUDIENCE,
        required_scopes=["officer"],
    ),
)


def current_officer() -> dict:
    """Return claims for the authenticated officer."""
    access_token = get_access_token()
    if access_token is None:
        raise ValueError("A valid officer access token is required.")

    try:
        return decode_officer_token(access_token.token)
    except Exception:
        raise ValueError("Invalid or expired officer access token.") from None


@mcp.tool()
def list_pending_applications(
    block: str | None = None,
    limit: int = 50,
) -> dict:
    """List pending pension applications, optionally filtered by block.

    Args:
        block: Optional permitted block name.
        limit: Maximum number of records to return, from 1 to 100.
    """
    current_officer()

    limit = max(1, min(int(limit), 100))

    if block is not None:
        block = block.strip()
        if not block:
            block = None
        elif block not in portal.BLOCKS:
            return {
                "success": False,
                "message": "Unknown block.",
                "valid_blocks": portal.BLOCKS,
            }

    conn = portal.get_db()
    cur = conn.cursor()
    try:
        if block:
            cur.execute(
                """
                SELECT application_no, applicant_name, dob, village, block,
                       submitted_at
                FROM applications
                WHERE status = 'PENDING' AND block = %s
                ORDER BY submitted_at ASC
                LIMIT %s
                """,
                (block, limit),
            )
        else:
            cur.execute(
                """
                SELECT application_no, applicant_name, dob, village, block,
                       submitted_at
                FROM applications
                WHERE status = 'PENDING'
                ORDER BY submitted_at ASC
                LIMIT %s
                """,
                (limit,),
            )

        rows = cur.fetchall()

        return {
            "success": True,
            "count": len(rows),
            "applications": [
                {
                    "application_number": row[0],
                    "applicant_name": row[1],
                    "date_of_birth": row[2].isoformat() if row[2] else None,
                    "village": row[3],
                    "block": row[4],
                    "submitted_at": row[5].isoformat() if row[5] else None,
                }
                for row in rows
            ],
        }
    finally:
        cur.close()
        conn.close()


@mcp.tool()
def inspect_application(application_number: str) -> dict:
    """Inspect an application and its audit history by application number."""
    current_officer()

    application_number = application_number.strip()
    if not application_number:
        return {"success": False, "message": "Application number is required."}

    conn = portal.get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT id, application_no, applicant_name, mobile, dob, gender,
                   marital_status, husband_name, village, block, bank_account,
                   ifsc, doc_path, status, submitted_at, decided_at, decided_by
            FROM applications
            WHERE application_no = %s
            """,
            (application_number,),
        )
        row = cur.fetchone()

        if not row:
            return {
                "success": False,
                "found": False,
                "message": "No application was found with that number.",
            }

        cur.execute(
            """
            SELECT at, action, actor, note
            FROM audit_log
            WHERE application_id = %s
            ORDER BY at ASC
            """,
            (row[0],),
        )
        audit_rows = cur.fetchall()

        return {
            "success": True,
            "found": True,
            "application": {
                "application_number": row[1],
                "applicant_name": row[2],
                "mobile": row[3],
                "date_of_birth": row[4].isoformat() if row[4] else None,
                "gender": row[5],
                "marital_status": row[6],
                "husband_name": row[7],
                "village": row[8],
                "block": row[9],
                "bank_account": row[10],
                "ifsc": row[11],
                "age_proof_on_file": bool(row[12]),
                "status": row[13],
                "submitted_at": row[14].isoformat() if row[14] else None,
                "decided_at": row[15].isoformat() if row[15] else None,
                "decided_by": row[16],
            },
            "audit_history": [
                {
                    "at": item[0].isoformat() if item[0] else None,
                    "action": item[1],
                    "actor": item[2],
                    "reason": item[3],
                }
                for item in audit_rows
            ],
        }
    finally:
        cur.close()
        conn.close()


@mcp.tool()
def decide_application(
    application_number: str,
    decision: str,
    reason: str,
) -> dict:
    """Approve or reject a pending application and record the officer's reason.

    Args:
        application_number: Existing application number.
        decision: Either APPROVED or REJECTED.
        reason: Non-empty explanation recorded in the audit log.
    """
    claims = current_officer()
    application_number = application_number.strip()
    decision = decision.strip().upper()
    reason = reason.strip()

    if not application_number:
        return {"success": False, "message": "Application number is required."}

    if decision not in ("APPROVED", "REJECTED"):
        return {
            "success": False,
            "message": "Decision must be APPROVED or REJECTED.",
        }

    if not reason:
        return {
            "success": False,
            "message": "A reason is required for every decision.",
        }

    if len(reason) > 1000:
        return {
            "success": False,
            "message": "Reason must be 1000 characters or fewer.",
        }

    username = claims["sub"].partition(":")[2]
    conn = portal.get_db()
    cur = conn.cursor()

    try:
        # Lock the row so two concurrent decisions cannot both succeed.
        cur.execute(
            """
            SELECT id, status
            FROM applications
            WHERE application_no = %s
            FOR UPDATE
            """,
            (application_number,),
        )
        row = cur.fetchone()

        if not row:
            conn.rollback()
            return {
                "success": False,
                "message": "Application not found.",
            }

        app_id, current_status = row

        if current_status != "PENDING":
            conn.rollback()
            return {
                "success": False,
                "message": "Only pending applications can be decided.",
                "current_status": current_status,
            }

        now = datetime.now()
        cur.execute(
            """
            UPDATE applications
            SET status = %s, decided_at = %s, decided_by = %s
            WHERE id = %s AND status = 'PENDING'
            RETURNING application_no
            """,
            (decision, now, username, app_id),
        )
        updated = cur.fetchone()

        if not updated:
            conn.rollback()
            return {
                "success": False,
                "message": "The application could not be updated; no decision was recorded.",
            }

        portal.write_audit(
            cur,
            app_id,
            decision,
            "admin:" + username,
            reason,
        )
        conn.commit()

        return {
            "success": True,
            "application_number": updated[0],
            "decision": decision,
            "decided_by": username,
            "reason_recorded": reason,
            "message": "The decision was saved to the portal and audit log.",
        }

    except Exception:
        conn.rollback()
        logger.exception("Officer decision failed")
        raise ValueError(
            "The decision could not be saved. Check the application and try again."
        ) from None
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    logger.info("Starting officer MCP server on port %s", PORT)
    mcp.run(transport="streamable-http")
