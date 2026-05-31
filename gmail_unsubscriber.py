#!/usr/bin/env python3
"""
Gmail Retail Email Unsubscriber

Scans your Gmail for retail/promotional emails, extracts unsubscribe
information from List-Unsubscribe headers, and executes unsubscription
via mailto or HTTP methods.

Setup:
  1. Go to https://console.cloud.google.com/
  2. Create a project, enable Gmail API, create OAuth 2.0 credentials
     (Desktop app), and download as credentials.json in this directory.
  3. pip install -r requirements.txt
  4. python gmail_unsubscriber.py [--dry-run] [--limit N]
"""

import argparse
import base64
import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from urllib.parse import urlparse

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

# Senders that are clearly transactional (keep these)
TRANSACTIONAL_PATTERNS = [
    r"noreply@.*bank",
    r"noreply@.*gov",
    r"no-?reply@.*hospital",
    r"security@",
    r"alerts@",
]


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


def get_header(headers: list[dict], name: str) -> str | None:
    name_lower = name.lower()
    for h in headers:
        if h["name"].lower() == name_lower:
            return h["value"]
    return None


def parse_unsubscribe_header(header_value: str) -> dict:
    """Return {'mailto': ..., 'http': ...} from List-Unsubscribe header."""
    result = {}
    # Header may contain multiple comma-separated angle-bracket values
    for match in re.finditer(r"<([^>]+)>", header_value):
        url = match.group(1).strip()
        if url.startswith("mailto:"):
            result["mailto"] = url
        elif url.startswith("http"):
            result["http"] = url
    return result


def is_transactional(sender: str) -> bool:
    for pattern in TRANSACTIONAL_PATTERNS:
        if re.search(pattern, sender, re.IGNORECASE):
            return True
    return False


def unsubscribe_via_http(url: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"    [dry-run] Would POST/GET {url}")
        return True
    try:
        resp = requests.post(url, timeout=10, allow_redirects=True)
        if resp.status_code < 400:
            return True
        # Some endpoints only accept GET
        resp = requests.get(url, timeout=10, allow_redirects=True)
        return resp.status_code < 400
    except requests.RequestException as e:
        print(f"    HTTP error: {e}")
        return False


def unsubscribe_via_mailto(
    mailto: str, sender_email: str, service: object, dry_run: bool
) -> bool:
    """Send an unsubscribe email using Gmail's send API."""
    # Parse mailto:address?subject=...&body=...
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
                    subject = requests.utils.unquote(val)
                elif key.lower() == "body":
                    body = requests.utils.unquote(val)

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


def fetch_retail_threads(service: object, limit: int) -> list[dict]:
    threads = []
    page_token = None
    while len(threads) < limit:
        batch_size = min(100, limit - len(threads))
        kwargs = {
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


def process_threads(
    service: object, threads: list[dict], dry_run: bool
) -> dict:
    seen_senders: set[str] = set()
    results = {"unsubscribed": [], "skipped": [], "failed": []}

    for i, thread in enumerate(threads, 1):
        try:
            full = service.users().threads().get(
                userId="me", id=thread["id"], format="metadata",
                metadataHeaders=["From", "List-Unsubscribe", "List-Unsubscribe-Post"]
            ).execute()
        except HttpError as e:
            print(f"  Error fetching thread {thread['id']}: {e}")
            continue

        # Use the first (oldest) message's headers for the sender
        messages = full.get("messages", [])
        if not messages:
            continue
        headers = messages[0].get("payload", {}).get("headers", [])
        sender = get_header(headers, "From") or ""
        unsub_header = get_header(headers, "List-Unsubscribe") or ""

        # Deduplicate by sender
        sender_key = re.sub(r".*<(.+)>.*", r"\1", sender).strip().lower() or sender.lower()
        if sender_key in seen_senders:
            continue
        seen_senders.add(sender_key)

        print(f"[{i}/{len(threads)}] {sender[:70]}")

        if not unsub_header:
            print("    No List-Unsubscribe header — skipping")
            results["skipped"].append({"sender": sender, "reason": "no header"})
            continue

        if is_transactional(sender):
            print("    Looks transactional — skipping")
            results["skipped"].append({"sender": sender, "reason": "transactional"})
            continue

        methods = parse_unsubscribe_header(unsub_header)
        if not methods:
            print(f"    Could not parse header: {unsub_header[:80]}")
            results["skipped"].append({"sender": sender, "reason": "unparseable header"})
            continue

        success = False
        # Prefer HTTP (RFC 8058 one-click) over mailto
        if "http" in methods:
            print(f"    Unsubscribing via HTTP: {methods['http'][:60]}")
            success = unsubscribe_via_http(methods["http"], dry_run)
        elif "mailto" in methods:
            print(f"    Unsubscribing via mailto: {methods['mailto'][:60]}")
            success = unsubscribe_via_mailto(methods["mailto"], sender, service, dry_run)

        if success:
            print("    OK")
            results["unsubscribed"].append(sender)
        else:
            print("    FAILED")
            results["failed"].append(sender)

        time.sleep(0.3)  # be polite to Gmail API rate limits

    return results


def label_unsubscribed(service: object, label_name: str = "Unsubscribed") -> str | None:
    """Create (or find) a Gmail label and return its ID."""
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


def print_summary(results: dict, dry_run: bool) -> None:
    prefix = "[DRY RUN] " if dry_run else ""
    print("\n" + "=" * 60)
    print(f"{prefix}SUMMARY")
    print("=" * 60)
    print(f"  Unsubscribed : {len(results['unsubscribed'])}")
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
        description="Unsubscribe from retail/promotional Gmail threads."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be unsubscribed without actually doing it.",
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
    print(f"Scanning up to {args.limit} promotional threads...")
    threads = fetch_retail_threads(service, args.limit)
    print(f"Found {len(threads)} threads. Processing unique senders...\n")

    results = process_threads(service, threads, dry_run=args.dry_run)
    print_summary(results, dry_run=args.dry_run)

    # Save report
    report_path = "unsubscribe_report.json"
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nReport saved to {report_path}")


if __name__ == "__main__":
    main()
