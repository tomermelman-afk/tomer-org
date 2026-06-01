#!/usr/bin/env python3
"""
Gmail Unsubscriber — like Unroll.me but self-hosted.

Scans Gmail for promotional/newsletter emails, extracts unsubscribe
information from List-Unsubscribe headers (and optionally from the email
body), then lets you interactively or automatically unsubscribe.

Setup:
  1. Go to https://console.cloud.google.com/
  2. Create a project, enable Gmail API, create OAuth 2.0 credentials
     (Desktop app), and download as credentials.json in this directory.
  3. pip install -r requirements.txt
  4. python gmail_unsubscriber.py [--dry-run] [--interactive] [--limit N]
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from email.mime.text import MIMEText
from html.parser import HTMLParser
from urllib.parse import unquote

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"

RETAIL_SEARCH_QUERY = (
    "category:promotions OR "
    "(subject:(sale OR offer OR discount OR promo OR coupon OR deal OR "
    "newsletter OR unsubscribe OR \"free shipping\" OR clearance OR "
    "\"% off\" OR shop OR order OR receipt OR \"thank you for your purchase\"))"
)

TRANSACTIONAL_PATTERNS = [
    r"noreply@.*bank",
    r"noreply@.*gov",
    r"no-?reply@.*hospital",
    r"security@",
    r"alerts@",
]

_UNSUB_HREF_RE = re.compile(
    r'href=["\']([^"\']*unsubscri[^"\']*)["\']',
    re.IGNORECASE,
)
_UNSUB_TEXT_URL_RE = re.compile(
    r'https?://[^\s<>"\']+unsubscri[^\s<>"\']*',
    re.IGNORECASE,
)


class _UnsubscribeLinkExtractor(HTMLParser):
    """Collect hrefs that look like unsubscribe links."""

    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href and re.search(r"unsubscri", href, re.IGNORECASE):
                self.links.append(href)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def authenticate() -> object:
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                print(
                    f"ERROR: {CREDENTIALS_FILE} not found.\n"
                    "Download OAuth 2.0 credentials from Google Cloud Console "
                    "(APIs & Services > Credentials > Create Credentials > "
                    "OAuth client ID > Desktop app) and save as credentials.json."
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------

def get_header(headers: list[dict], name: str) -> str | None:
    name_lower = name.lower()
    for h in headers:
        if h["name"].lower() == name_lower:
            return h["value"]
    return None


def parse_unsubscribe_header(header_value: str) -> dict:
    """Return {'mailto': ..., 'http': ...} from a List-Unsubscribe header."""
    result = {}
    for match in re.finditer(r"<([^>]+)>", header_value):
        url = match.group(1).strip()
        if url.startswith("mailto:"):
            result["mailto"] = url
        elif url.startswith("http"):
            result["http"] = url
    return result


def is_transactional(sender: str) -> bool:
    return any(re.search(p, sender, re.IGNORECASE) for p in TRANSACTIONAL_PATTERNS)


# ---------------------------------------------------------------------------
# Body scanning
# ---------------------------------------------------------------------------

def _collect_parts(payload: dict) -> list[tuple[str, str]]:
    """Recursively collect (mimeType, body.data) pairs from a message payload."""
    results = []
    data = payload.get("body", {}).get("data")
    if data:
        results.append((payload.get("mimeType", ""), data))
    for part in payload.get("parts", []):
        results.extend(_collect_parts(part))
    return results


def extract_body_unsubscribe_link(service: object, message_id: str) -> str | None:
    """Fetch a full message and return the first unsubscribe URL found in the body."""
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
    except HttpError:
        return None

    for mime_type, data in _collect_parts(msg.get("payload", {})):
        try:
            text = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        except Exception:
            continue
        if "html" in mime_type:
            parser = _UnsubscribeLinkExtractor()
            parser.feed(text)
            if parser.links:
                return parser.links[0]
            m = _UNSUB_HREF_RE.search(text)
            if m:
                return m.group(1)
        m = _UNSUB_TEXT_URL_RE.search(text)
        if m:
            return m.group(0)
    return None


# ---------------------------------------------------------------------------
# Unsubscribe actions
# ---------------------------------------------------------------------------

def unsubscribe_via_http(url: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"    [dry-run] Would POST/GET {url}")
        return True
    try:
        resp = requests.post(url, timeout=10, allow_redirects=True)
        if resp.status_code < 400:
            return True
        # Some endpoints only support GET
        resp = requests.get(url, timeout=10, allow_redirects=True)
        return resp.status_code < 400
    except requests.RequestException as e:
        print(f"    HTTP error: {e}")
        return False


def unsubscribe_via_mailto(mailto: str, service: object, dry_run: bool) -> bool:
    """Send an unsubscribe email via Gmail's send API."""
    mailto_clean = mailto[len("mailto:"):]
    parts = mailto_clean.split("?", 1)
    to_addr = parts[0]
    subject = "Unsubscribe"
    body = ""
    if len(parts) > 1:
        for param in parts[1].split("&"):
            if "=" in param:
                key, val = param.split("=", 1)
                if key.lower() == "subject":
                    subject = unquote(val)
                elif key.lower() == "body":
                    body = unquote(val)

    if dry_run:
        print(f"    [dry-run] Would send unsubscribe email to {to_addr} (subject: {subject})")
        return True

    try:
        message = MIMEText(body or "Please unsubscribe me from this mailing list.")
        message["To"] = to_addr
        message["Subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True
    except HttpError as e:
        print(f"    Gmail send error: {e}")
        return False


# ---------------------------------------------------------------------------
# Gmail label / archive helpers
# ---------------------------------------------------------------------------

def get_or_create_label(service: object, label_name: str) -> str | None:
    try:
        existing = service.users().labels().list(userId="me").execute().get("labels", [])
        for lbl in existing:
            if lbl["name"].lower() == label_name.lower():
                return lbl["id"]
        created = service.users().labels().create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ).execute()
        return created["id"]
    except HttpError:
        return None


def archive_sender_emails(service: object, sender_email: str, dry_run: bool) -> int:
    """Remove INBOX label from all messages from this sender. Returns count archived."""
    if dry_run:
        print(f"    [dry-run] Would archive emails from {sender_email}")
        return 0
    archived = 0
    page_token = None
    while True:
        kwargs: dict = {"userId": "me", "q": f"from:{sender_email} in:inbox", "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            resp = service.users().messages().list(**kwargs).execute()
        except HttpError as e:
            print(f"    Archive list error: {e}")
            break
        messages = resp.get("messages", [])
        if messages:
            try:
                service.users().messages().batchModify(
                    userId="me",
                    body={"ids": [m["id"] for m in messages], "removeLabelIds": ["INBOX"]},
                ).execute()
                archived += len(messages)
            except HttpError as e:
                print(f"    Archive modify error: {e}")
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return archived


# ---------------------------------------------------------------------------
# Thread fetching
# ---------------------------------------------------------------------------

def fetch_retail_threads(service: object, limit: int) -> list[dict]:
    threads: list[dict] = []
    page_token = None
    while len(threads) < limit:
        batch_size = min(100, limit - len(threads))
        kwargs: dict = {
            "userId": "me",
            "q": RETAIL_SEARCH_QUERY,
            "maxResults": batch_size,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            resp = service.users().threads().list(**kwargs).execute()
        except HttpError as e:
            print(f"Error listing threads: {e}")
            break
        threads.extend(resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return threads


# ---------------------------------------------------------------------------
# Interactive prompt
# ---------------------------------------------------------------------------

def prompt_user(sender: str, method_desc: str) -> str:
    """Return 'u' (unsubscribe), 'k' (keep), or 'q' (quit)."""
    print(f"  Sender : {sender}")
    print(f"  Method : {method_desc}")
    while True:
        try:
            choice = input("  Action? [U]nsubscribe / [K]eep / [Q]uit : ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "q"
        if choice in ("u", "k", "q"):
            return choice
        if choice == "":
            return "k"


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_threads(
    service: object,
    threads: list[dict],
    dry_run: bool,
    interactive: bool,
    scan_body: bool,
    archive: bool,
    label_id: str | None,
) -> dict:
    seen_senders: set[str] = set()
    results: dict = {"unsubscribed": [], "kept": [], "skipped": [], "failed": []}

    for i, thread in enumerate(threads, 1):
        try:
            full = service.users().threads().get(
                userId="me",
                id=thread["id"],
                format="metadata",
                metadataHeaders=["From", "List-Unsubscribe", "List-Unsubscribe-Post"],
            ).execute()
        except HttpError as e:
            print(f"  Error fetching thread {thread['id']}: {e}")
            continue

        messages = full.get("messages", [])
        if not messages:
            continue

        headers = messages[0].get("payload", {}).get("headers", [])
        sender = get_header(headers, "From") or ""
        unsub_header = get_header(headers, "List-Unsubscribe") or ""

        sender_key = re.sub(r".*<(.+)>.*", r"\1", sender).strip().lower() or sender.lower()
        if sender_key in seen_senders:
            continue
        seen_senders.add(sender_key)

        print(f"\n[{i}/{len(threads)}] {sender[:80]}")

        if is_transactional(sender):
            print("    Looks transactional — skipping")
            results["skipped"].append({"sender": sender, "reason": "transactional"})
            continue

        methods: dict = {}
        method_desc = ""

        if unsub_header:
            methods = parse_unsubscribe_header(unsub_header)
            if "http" in methods:
                method_desc = f"HTTP one-click: {methods['http'][:60]}"
            elif "mailto" in methods:
                method_desc = f"mailto: {methods['mailto'][:60]}"

        if not methods and scan_body:
            print("    No List-Unsubscribe header — scanning body...")
            first_msg_id = messages[0].get("id")
            body_url = extract_body_unsubscribe_link(service, first_msg_id) if first_msg_id else None
            if body_url:
                methods = {"http": body_url}
                method_desc = f"body link: {body_url[:60]}"

        if not methods:
            print("    No unsubscribe method found — skipping")
            results["skipped"].append({"sender": sender, "reason": "no unsubscribe method"})
            continue

        if interactive:
            choice = prompt_user(sender, method_desc)
            if choice == "q":
                print("\nQuitting early.")
                break
            if choice == "k":
                print("    Keeping.")
                results["kept"].append(sender)
                continue

        # Execute unsubscription
        success = False
        if "http" in methods:
            print(f"    Unsubscribing via HTTP: {methods['http'][:60]}")
            success = unsubscribe_via_http(methods["http"], dry_run)
        elif "mailto" in methods:
            print(f"    Unsubscribing via mailto: {methods['mailto'][:60]}")
            success = unsubscribe_via_mailto(methods["mailto"], service, dry_run)

        if success:
            print("    OK")
            results["unsubscribed"].append(sender)
            if archive and sender_key:
                n = archive_sender_emails(service, sender_key, dry_run)
                if n:
                    print(f"    Archived {n} email(s) from this sender")
            if label_id and not dry_run:
                try:
                    service.users().messages().batchModify(
                        userId="me",
                        body={
                            "ids": [m["id"] for m in messages],
                            "addLabelIds": [label_id],
                        },
                    ).execute()
                except HttpError:
                    pass
        else:
            print("    FAILED")
            results["failed"].append(sender)

        time.sleep(0.3)

    return results


# ---------------------------------------------------------------------------
# Summary + entry point
# ---------------------------------------------------------------------------

def print_summary(results: dict, dry_run: bool) -> None:
    prefix = "[DRY RUN] " if dry_run else ""
    print("\n" + "=" * 60)
    print(f"{prefix}SUMMARY")
    print("=" * 60)
    print(f"  Unsubscribed : {len(results['unsubscribed'])}")
    print(f"  Kept         : {len(results.get('kept', []))}")
    print(f"  Skipped      : {len(results['skipped'])}")
    print(f"  Failed       : {len(results['failed'])}")
    if results["unsubscribed"]:
        print("\nUnsubscribed from:")
        for s in results["unsubscribed"]:
            print(f"  - {s}")
    if results["failed"]:
        print("\nFailed (manual action may be needed):")
        for s in results["failed"]:
            print(f"  - {s}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unsubscribe from promotional/newsletter Gmail threads.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview what would be unsubscribed
  python gmail_unsubscriber.py --dry-run

  # Choose interactively for each sender
  python gmail_unsubscriber.py --interactive

  # Auto-unsubscribe, archive cleaned emails, scan body links too
  python gmail_unsubscriber.py --archive --scan-body

  # Label unsubscribed threads and limit to 50 threads
  python gmail_unsubscriber.py --label --limit 50
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without actually doing it.",
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Prompt [U]nsubscribe / [K]eep / [Q]uit for each unique sender.",
    )
    parser.add_argument(
        "--scan-body",
        action="store_true",
        help="When no List-Unsubscribe header is found, scan the email body for unsubscribe links.",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Archive (remove from inbox) all emails from successfully unsubscribed senders.",
    )
    parser.add_argument(
        "--label",
        metavar="LABEL",
        nargs="?",
        const="Unsubscribed",
        help="Apply a Gmail label to processed threads (default name: 'Unsubscribed').",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Max number of threads to scan (default: 200).",
    )
    args = parser.parse_args()

    print("Authenticating with Gmail...")
    service = authenticate()

    label_id = None
    if args.label:
        label_id = get_or_create_label(service, args.label)
        if label_id:
            print(f"Using Gmail label: '{args.label}'")
        else:
            print(f"Warning: could not create/find label '{args.label}'")

    print(f"Scanning up to {args.limit} promotional threads...")
    threads = fetch_retail_threads(service, args.limit)
    print(f"Found {len(threads)} threads. Processing unique senders...\n")

    results = process_threads(
        service,
        threads,
        dry_run=args.dry_run,
        interactive=args.interactive,
        scan_body=args.scan_body,
        archive=args.archive,
        label_id=label_id,
    )
    print_summary(results, dry_run=args.dry_run)

    report_path = "unsubscribe_report.json"
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nReport saved to {report_path}")


if __name__ == "__main__":
    main()
