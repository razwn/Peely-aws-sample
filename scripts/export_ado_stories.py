#!/usr/bin/env python3
"""Export Azure DevOps work items (titles + descriptions) to Markdown files.

Usage:
  export AZURE_DEVOPS_PAT=xxxxx
  python3 scripts/export_ado_stories.py \
      --org-url https://dev.azure.com/VFGroup-TVOIS-RO-SWE \
      --project VDLP \
      --types "User Story" \
      --output-dir ./ado_export

The resulting .md files can be zipped/uploaded directly into a chatbot
workspace (RAG) as business-context documents.
"""

import argparse
import base64
import html.parser
import json
import os
import re
import sys
import urllib.error
import urllib.request

API_VERSION = "7.1"


class HTMLTextExtractor(html.parser.HTMLParser):
    """Minimal HTML -> plain text converter (no extra dependencies)."""

    BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.chunks.append("\n- ")
        elif tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data):
        self.chunks.append(data)

    def get_text(self):
        text = "".join(self.chunks)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(value: str) -> str:
    if not value:
        return ""
    extractor = HTMLTextExtractor()
    extractor.feed(value)
    return extractor.get_text()


def sanitize_filename(value: str, max_len: int = 80) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return value[:max_len] or "untitled"


def ado_request(url: str, pat: str, method: str = "GET", body: dict = None) -> dict:
    token = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"ADO API error {e.code} for {url}: {detail}")


def fetch_work_item_ids(org_url: str, project: str, pat: str, types: list) -> list:
    type_clause = " OR ".join(f"[System.WorkItemType] = '{t}'" for t in types)
    wiql = {
        "query": (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{project}' AND ({type_clause}) "
            "ORDER BY [System.ChangedDate] DESC"
        )
    }
    url = f"{org_url}/{project}/_apis/wit/wiql?api-version={API_VERSION}"
    result = ado_request(url, pat, method="POST", body=wiql)
    return [item["id"] for item in result.get("workItems", [])]


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def fetch_work_items(org_url: str, pat: str, ids: list) -> list:
    fields = [
        "System.Id",
        "System.Title",
        "System.WorkItemType",
        "System.State",
        "System.Tags",
        "System.AreaPath",
        "System.IterationPath",
        "System.Description",
        "Microsoft.VSTS.Common.AcceptanceCriteria",
    ]
    url = f"{org_url}/_apis/wit/workitemsbatch?api-version={API_VERSION}"
    items = []
    for batch_ids in chunked(ids, 200):
        body = {"ids": batch_ids, "fields": fields}
        result = ado_request(url, pat, method="POST", body=body)
        items.extend(result.get("value", []))
    return items


def render_markdown(item: dict) -> str:
    fields = item.get("fields", {})
    title = fields.get("System.Title", "Untitled")
    work_id = item.get("id")
    work_type = fields.get("System.WorkItemType", "")
    state = fields.get("System.State", "")
    tags = fields.get("System.Tags", "")
    area = fields.get("System.AreaPath", "")
    iteration = fields.get("System.IterationPath", "")
    description = html_to_text(fields.get("System.Description", ""))
    acceptance = html_to_text(fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", ""))

    lines = [
        f"# {title}",
        "",
        f"- **ID:** {work_id}",
        f"- **Type:** {work_type}",
        f"- **State:** {state}",
        f"- **Area:** {area}",
        f"- **Iteration:** {iteration}",
        f"- **Tags:** {tags}",
        "",
        "## Description",
        description or "_No description provided._",
    ]
    if acceptance:
        lines += ["", "## Acceptance Criteria", acceptance]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-url", default=os.environ.get("ADO_ORG_URL", "https://dev.azure.com/VFGroup-TVOIS-RO-SWE"))
    parser.add_argument("--project", default=os.environ.get("ADO_PROJECT", "VDLP"))
    parser.add_argument("--types", default="User Story", help="Comma-separated work item types")
    parser.add_argument("--output-dir", default="./ado_export")
    parser.add_argument("--pat", default=os.environ.get("AZURE_DEVOPS_PAT"))
    args = parser.parse_args()

    if not args.pat:
        sys.exit("Set AZURE_DEVOPS_PAT env var (or pass --pat) with a PAT that has Work Items (Read) scope.")

    types = [t.strip() for t in args.types.split(",") if t.strip()]
    os.makedirs(args.output_dir, exist_ok=True)

    print(f"Querying {args.project} for types: {', '.join(types)}")
    ids = fetch_work_item_ids(args.org_url, args.project, args.pat, types)
    print(f"Found {len(ids)} work items")
    if not ids:
        return

    items = fetch_work_items(args.org_url, args.pat, ids)
    for item in items:
        fields = item.get("fields", {})
        filename = f"{item['id']}_{sanitize_filename(fields.get('System.Title', 'untitled'))}.md"
        path = os.path.join(args.output_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_markdown(item))

    print(f"Wrote {len(items)} files to {args.output_dir}")


if __name__ == "__main__":
    main()
