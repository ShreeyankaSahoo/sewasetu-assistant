import base64
import logging
import os
import re
import sys
import uuid
from datetime import datetime

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, "/app")
import app as portal


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sewasetu.citizen-mcp")

HOST = "0.0.0.0"
PORT = int(os.getenv("MCP_PORT", "8001"))
AUDIENCE = os.getenv("MCP_AUDIENCE", "http://localhost:8001")
ISSUER = os.getenv("PORTAL_ISSUER", "http://localhost:8000")
JWKS_URL = os.getenv(
    "JWKS_URL", "http://app:8000/.well-known/jwks.json"
)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
jwks_client = PyJWKClient(JWKS_URL)


def decode_portal_token(token: str) -> dict:
    """Verify the portal's JWT signature and required claims."""
    key = jwks_client.get_signing_key_from_jwt(token)

    claims = jwt.decode(
        token,
        key.key,
        algorithms=["RS256"],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": ["iss", "sub", "aud", "iat", "exp"]},
    )

    if (
        claims.get("role") != "citizen"
        or claims.get("scope") != "citizen"
        or claims.get("kind") != "access"
    ):
        raise ValueError("A valid citizen access token is required.")

    subject = claims.get("sub", "")
    if not isinstance(subject, str) or not subject.startswith("citizen:"):
        raise ValueError("The token does not identify a citizen.")

    if not subject.partition(":")[2]:
        raise ValueError("Citizen identity is missing.")

    return claims


def current_citizen() -> tuple[dict, str]:
    """Get the identity from the SDK's verified authentication context."""
    access_token = get_access_token()
    if access_token is None:
        raise ValueError("A valid citizen access token is required.")

    try:
        claims = decode_portal_token(access_token.token)
    except Exception:
        raise ValueError("The citizen token is invalid or expired.") from None

    mobile = claims["sub"].partition(":")[2]
    return claims, mobile



class SewaSetuCitizenTokenVerifier(TokenVerifier):
    """MCP SDK verifier for citizen-only Sewa Setu tokens."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = decode_portal_token(token)

            return AccessToken(
                token=token,
                client_id=str(claims.get("client_id", "")),
                scopes=["citizen"],
                expires_at=int(claims["exp"]),
                resource=AUDIENCE,
            )

        except Exception as exc:
            logger.warning(
                "Citizen token verification failed: %s: %s",
                type(exc).__name__,
                str(exc),
            )
            return None



mcp = FastMCP(
    name="Sewa Setu Citizen Assistant",
    instructions=(
        "Citizen-only pension assistance. Use the authenticated citizen's "
        "identity. Never read or alter another person's application. Use "
        "portal validation before submitting. Eligibility guidance is not "
        "an approval, and a successful submission must return an application number."
    ),
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
    token_verifier=SewaSetuCitizenTokenVerifier(),
    auth=AuthSettings(
        issuer_url=ISSUER,
        resource_server_url=AUDIENCE,
        required_scopes=["citizen"],
    ),
)


def owned_upload_path(mobile: str, document_id: str) -> str:
    """Resolve an uploaded document only if it belongs to this citizen."""
    if not document_id or os.path.basename(document_id) != document_id:
        raise ValueError("Invalid document ID. Upload age proof first.")

    prefix = re.sub(r"\D", "", mobile)
    if not prefix:
        raise ValueError("The authenticated mobile number is invalid.")

    pattern = (
        re.escape(prefix)
        + r"_[0-9a-f]{32}\.(?:pdf|jpg|jpeg|png)"
    )
    if not re.fullmatch(pattern, document_id, flags=re.IGNORECASE):
        raise ValueError("That document does not belong to your verified account.")

    root = os.path.realpath(portal.UPLOAD_DIR)
    path = os.path.realpath(os.path.join(root, document_id))

    if os.path.commonpath([root, path]) != root or not os.path.isfile(path):
        raise ValueError("The uploaded document was not found. Upload it again.")

    if os.path.getsize(path) > MAX_UPLOAD_BYTES:
        raise ValueError("The document exceeds the 5 MB limit.")

    return path


@mcp.tool()
def check_eligibility(
    date_of_birth: str,
    village: str,
    block: str,
) -> dict:
    """Check preliminary age, village, block, and application-window rules.

    Date of birth must be DD/MM/YYYY. This does not guarantee approval.
    """
    dob = portal.parse_dob(date_of_birth)

    if dob is None:
        return {
            "eligible_by_age": False,
            "message": "Enter the date of birth in DD/MM/YYYY format.",
        }

    age = portal.age_on(dob, portal.now_ist().date())
    valid_block = block in portal.BLOCKS
    has_village = bool(village and village.strip())
    window_open = portal.now_ist() <= portal.SCHEME_DEADLINE

    passed = (
        age >= portal.MIN_AGE
        and valid_block
        and has_village
        and window_open
    )

    return {
        "age": age,
        "minimum_age": portal.MIN_AGE,
        "eligible_by_age": age >= portal.MIN_AGE,
        "valid_block": valid_block,
        "valid_blocks": portal.BLOCKS,
        "village_provided": has_village,
        "application_window_open": window_open,
        "application_deadline": portal.SCHEME_DEADLINE.isoformat(),
        "preliminary_checks_passed": passed,
        "message": (
            "Preliminary checks passed. Full portal validation and age proof "
            "are still required."
            if passed
            else "One or more preliminary checks failed."
        ),
    }


@mcp.tool()
def upload_age_proof(filename: str, content_base64: str) -> dict:
    """Upload a PDF, JPG, JPEG, or PNG age-proof document, up to 5 MB.

    Args:
        filename: Original filename including its extension.
        content_base64: Base64-encoded file contents, without a data-URL prefix.

    The returned document_id is needed when submitting the application.
    """
    _, mobile = current_citizen()

    if not filename or not content_base64:
        return {
            "success": False,
            "message": "Provide a filename and base64-encoded file contents.",
        }

    # Never use the caller's original path to construct a server filepath.
    extension = os.path.splitext(os.path.basename(filename))[1].lower()
    if extension not in ALLOWED_EXTENSIONS:
        return {
            "success": False,
            "message": "Allowed formats are PDF, JPG, JPEG, and PNG.",
        }

    maximum_encoded_length = ((MAX_UPLOAD_BYTES + 2) // 3) * 4
    if len(content_base64) > maximum_encoded_length:
        return {
            "success": False,
            "message": "The file is larger than the 5 MB limit.",
        }

    try:
        content = base64.b64decode(content_base64, validate=True)
    except Exception:
        return {
            "success": False,
            "message": "Invalid base64 file contents.",
        }

    if not content:
        return {
            "success": False,
            "message": "The uploaded document is empty.",
        }

    if len(content) > MAX_UPLOAD_BYTES:
        return {
            "success": False,
            "message": "The file is larger than the 5 MB limit.",
        }

    prefix = re.sub(r"\D", "", mobile)
    if not prefix:
        raise ValueError("The authenticated mobile number is invalid.")

    os.makedirs(portal.UPLOAD_DIR, exist_ok=True)
    document_id = f"{prefix}_{uuid.uuid4().hex}{extension}"
    path = os.path.join(portal.UPLOAD_DIR, document_id)

    try:
        with open(path, "xb") as file:
            file.write(content)
    except OSError:
        logger.exception("Could not store citizen age proof")
        raise ValueError("Could not save the document. Please try again.") from None

    return {
        "success": True,
        "document_id": document_id,
        "bytes_received": len(content),
        "message": (
            "Document uploaded. Keep the document_id and use it to submit "
            "your pension application."
        ),
    }


@mcp.tool()
def submit_application(
    applicant_name: str,
    date_of_birth: str,
    village: str,
    block: str,
    bank_account: str,
    document_id: str,
    gender: str = "",
    marital_status: str = "",
    husband_name: str = "",
    ifsc: str = "",
) -> dict:
    """Submit a pension application through the portal's validation rules.

    Date of birth must be DD/MM/YYYY. Upload age proof first and pass the
    returned document_id. A submission creates a real PENDING portal record.
    """
    _, mobile = current_citizen()

    try:
        doc_path = owned_upload_path(mobile, document_id)
    except ValueError as exc:
        return {"success": False, "message": str(exc)}

    data = {
        "applicant_name": applicant_name,
        "dob": date_of_birth,
        "village": village,
        "block": block,
        "bank_account": bank_account,
        "ifsc": ifsc,
        "gender": gender,
        "marital_status": marital_status,
        "husband_name": husband_name,
    }

    errors, cleaned = portal.validate_application(data, doc_path)
    if errors:
        return {
            "success": False,
            "submitted": False,
            "validation_errors": errors,
            "message": "The portal rejected the application. Correct the listed issues.",
        }

    conn = portal.get_db()
    cur = conn.cursor()
    try:
        existing = portal.active_application(cur, mobile)
        if existing:
            conn.rollback()
            return {
                "success": False,
                "submitted": False,
                "existing_application_number": existing[1],
                "existing_status": existing[2],
                "message": (
                    "An active application already exists for this verified mobile "
                    "number. Check its status before attempting another submission."
                ),
            }

        new_id, app_no = portal.create_application(cur, mobile, cleaned)
        conn.commit()

    except Exception:
        conn.rollback()
        logger.exception("Citizen application submission failed")
        raise ValueError(
            "The application could not be saved. Please retry after checking status."
        ) from None
    finally:
        cur.close()
        conn.close()

    # Send the portal's existing notification after the database commit.
    portal.send_sms(
        mobile,
        "Sewa Setu: application %s received. Track it at the status portal "
        "using your mobile number and DOB password." % app_no,
    )

    return {
        "success": True,
        "submitted": True,
        "application_number": app_no,
        "status": "PENDING",
        "message": "Your application has been submitted to Sewa Setu.",
    }


@mcp.tool()
def get_my_application() -> dict:
    """Return the latest application for the authenticated citizen."""
    _, mobile = current_citizen()

    conn = portal.get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT application_no, applicant_name, village, block, status,
                   submitted_at, decided_at
            FROM applications
            WHERE mobile = %s
            ORDER BY submitted_at DESC
            LIMIT 1
            """,
            (mobile,),
        )
        row = cur.fetchone()

        if not row:
            return {
                "found": False,
                "message": "No application was found for your verified account.",
            }

        return {
            "found": True,
            "application_number": row[0],
            "applicant_name": row[1],
            "village": row[2],
            "block": row[3],
            "status": row[4],
            "submitted_at": row[5].isoformat() if row[5] else None,
            "decided_at": row[6].isoformat() if row[6] else None,
        }
    finally:
        cur.close()
        conn.close()


@mcp.tool()
def withdraw_my_application(application_number: str) -> dict:
    """Withdraw your own pending application so you can apply again if needed."""
    _, mobile = current_citizen()
    application_number = (application_number or "").strip()

    if not application_number:
        return {
            "success": False,
            "message": "Application number is required.",
        }

    conn = portal.get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE applications
            SET status = 'WITHDRAWN', decided_at = %s, decided_by = 'APPLICANT'
            WHERE application_no = %s
              AND mobile = %s
              AND status = 'PENDING'
            RETURNING id, application_no
            """,
            (datetime.now(), application_number, mobile),
        )
        row = cur.fetchone()

        if not row:
            conn.rollback()
            return {
                "success": False,
                "message": (
                    "The application could not be withdrawn. It may not belong "
                    "to you, or it may no longer be pending."
                ),
            }

        portal.write_audit(
            cur,
            row[0],
            "WITHDRAW",
            "applicant:%s" % mobile,
        )
        conn.commit()

        return {
            "success": True,
            "application_number": row[1],
            "status": "WITHDRAWN",
            "message": "Your pending application has been withdrawn.",
        }

    except Exception:
        conn.rollback()
        logger.exception("Citizen withdrawal failed")
        raise ValueError(
            "The application could not be withdrawn. Please try again."
        ) from None
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    logger.info("Starting citizen MCP server on port %s", PORT)
    mcp.run(transport="streamable-http")