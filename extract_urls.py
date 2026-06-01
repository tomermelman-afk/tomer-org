#!/usr/bin/env python3
import json, re, sys

def extract_unsub_url(data):
    msgs = data.get("messages", [])
    if not msgs:
        return None
    msg = msgs[0]

    # 1. Check headers for List-Unsubscribe
    for h in msg.get("headers", []):
        if h.get("name","").lower() == "list-unsubscribe":
            val = h["value"]
            # prefer http over mailto
            http = re.search(r'<(https?://[^>]+)>', val)
            if http:
                return http.group(1)
            mailto = re.search(r'<(mailto:[^>]+)>', val)
            if mailto:
                return mailto.group(1)

    # 2. Scan HTML body for unsubscribe href
    html = msg.get("htmlBody", "") or ""
    # Find links containing "unsubscri"
    hrefs = re.findall(r'href=["\']([^"\']*unsubscri[^"\']*)["\']', html, re.IGNORECASE)
    if hrefs:
        # prefer http links
        for h in hrefs:
            if h.startswith("http"):
                return h
        return hrefs[0]

    # 3. Scan plain text for unsubscribe URLs
    text = msg.get("plaintextBody", "") or ""
    urls = re.findall(r'https?://[^\s<>"\')]+unsubscri[^\s<>"\')]*', text, re.IGNORECASE)
    if urls:
        return urls[0]

    return None

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: extract_urls.py <file_path>")
        sys.exit(1)

    file_path = sys.argv[1]
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Try to parse as JSON directly
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Maybe the file has extra content - try to find JSON object
            # Look for the JSON part
            match = re.search(r'(\{.*\})', content, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
            else:
                print("NONE")
                sys.exit(0)

        url = extract_unsub_url(data)
        print(url if url else "NONE")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("NONE")
