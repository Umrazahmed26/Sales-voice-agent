from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

from app.routers.webhooks import _send_unipile_message, _whatsapp_recipient


SUBMISSION_MESSAGE = """Hi, this is Umraz — submitting my working prototype for the SDE Intern assignment.

You can call me back to test it directly.

Resume: {resume_url}
Repo: {repo_url}
Architecture diagram: {architecture_diagram_url}
Reach me at: {sender_phone_number}

{short_note_text}"""


def _required_environment() -> dict[str, str]:
    names = (
        "UNIPILE_DSN",
        "UNIPILE_API_KEY",
        "UNIPILE_ACCOUNT_ID",
        "SUBMISSION_TARGET_NUMBER",
        "RESUME_URL",
        "REPO_URL",
        "ARCHITECTURE_DIAGRAM_URL",
        "SHORT_NOTE_TEXT",
        "SENDER_PHONE_NUMBER",
    )
    values = {name: os.getenv(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(f"Missing environment variables: {', '.join(missing)}")

    if len(values["SHORT_NOTE_TEXT"].split()) >= 200:
        raise ValueError("SHORT_NOTE_TEXT must be under 200 words")

    return values


def main() -> int:
    load_dotenv()

    try:
        values = _required_environment()
        message = SUBMISSION_MESSAGE.format(
            resume_url=values["RESUME_URL"],
            repo_url=values["REPO_URL"],
            architecture_diagram_url=values["ARCHITECTURE_DIAGRAM_URL"],
            sender_phone_number=values["SENDER_PHONE_NUMBER"],
            short_note_text=values["SHORT_NOTE_TEXT"],
        )
        response = _send_unipile_message(
            values["UNIPILE_DSN"],
            values["UNIPILE_API_KEY"],
            values["UNIPILE_ACCOUNT_ID"],
            _whatsapp_recipient(values["SUBMISSION_TARGET_NUMBER"]),
            message,
        )
    except ValueError as exc:
        print(f"Submission not sent: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json()
        except ValueError:
            detail = exc.response.text
        print(f"Submission failed ({exc.response.status_code}): {detail}", file=sys.stderr)
        return 1
    except httpx.RequestError as exc:
        print(f"Submission failed: could not reach Unipile: {exc}", file=sys.stderr)
        return 1

    print(f"Submission sent successfully to {_whatsapp_recipient(values['SUBMISSION_TARGET_NUMBER'])}.")
    print(f"Unipile response: {response.status_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
