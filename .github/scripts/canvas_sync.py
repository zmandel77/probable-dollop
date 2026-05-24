#!/usr/bin/env python3
"""Canvas LMS → Notion assignment sync."""

import os
import sys
import requests

CANVAS_BASE  = "https://UMassmed.instructure.com/api/v1"
CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
ASSIGNMENTS_DB = "7a892195dc594a9e8e1057b22428669f"

CANVAS_HDR = {"Authorization": f"Bearer {CANVAS_TOKEN}"}
NOTION_HDR = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# Canvas helpers
# ---------------------------------------------------------------------------

def canvas_get(path, params=None):
    """GET with auto-pagination; returns list of all results."""
    url = f"{CANVAS_BASE}{path}"
    results = []
    while url:
        r = requests.get(url, headers=CANVAS_HDR, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            return data
        url = r.links.get("next", {}).get("url")
        params = None  # only on first request
    return results


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------

def notion_query_all(filter_body=None):
    """Return all pages from the assignments DB."""
    results, cursor = [], None
    while True:
        body = {"page_size": 100}
        if filter_body:
            body["filter"] = filter_body
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{ASSIGNMENTS_DB}/query",
            headers=NOTION_HDR, json=body, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def notion_create(properties):
    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HDR,
        json={"parent": {"database_id": ASSIGNMENTS_DB}, "properties": properties},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def notion_update(page_id, properties):
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=NOTION_HDR, json={"properties": properties}, timeout=30,
    )
    r.raise_for_status()


def ensure_db_properties():
    """Add any missing properties to the Notion DB schema."""
    r = requests.get(
        f"https://api.notion.com/v1/databases/{ASSIGNMENTS_DB}",
        headers=NOTION_HDR, timeout=30,
    )
    r.raise_for_status()
    existing = set(r.json()["properties"].keys())

    needed = {
        "Canvas ID":        {"rich_text": {}},
        "Course":           {"select": {}},
        "Points Possible":  {"number": {"format": "number"}},
        "Score":            {"number": {"format": "number"}},
        "Submitted":        {"checkbox": {}},
        "Graded":           {"checkbox": {}},
        "Canvas URL":       {"url": {}},
        "Submission Type":  {"select": {}},
        "Locked":           {"checkbox": {}},
    }
    to_add = {k: v for k, v in needed.items() if k not in existing}
    if not to_add:
        return
    print(f"  Adding new Notion properties: {list(to_add.keys())}")
    r = requests.patch(
        f"https://api.notion.com/v1/databases/{ASSIGNMENTS_DB}",
        headers=NOTION_HDR, json={"properties": to_add}, timeout=30,
    )
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Property builders
# ---------------------------------------------------------------------------

def _txt(val):
    return {"rich_text": [{"text": {"content": str(val)[:2000]}}]}

def _select(val):
    return {"select": {"name": str(val)[:100]}}

def _num(val):
    return {"number": float(val)} if val is not None else {"number": None}


def build_properties(asgn, course_name):
    sub  = asgn.get("submission") or {}
    wf   = sub.get("workflow_state", "unsubmitted")
    submitted = wf in ("submitted", "graded", "pending_review")
    graded    = wf == "graded"
    score     = sub.get("score")
    locked    = bool(asgn.get("locked_for_user", False))

    sub_types = asgn.get("submission_types") or []
    sub_type  = sub_types[0].replace("_", " ").title() if sub_types else "Assignment"

    props = {
        "Name":             {"title": [{"text": {"content": asgn["name"][:2000]}}]},
        "Canvas ID":        _txt(asgn["id"]),
        "Course":           _select(course_name),
        "Points Possible":  _num(asgn.get("points_possible")),
        "Submitted":        {"checkbox": submitted},
        "Graded":           {"checkbox": graded},
        "Score":            _num(score),
        "Submission Type":  _select(sub_type),
        "Locked":           {"checkbox": locked},
    }

    due = asgn.get("due_at")
    props["Due Date"] = {"date": {"start": due[:10]}} if due else {"date": None}

    url = asgn.get("html_url", "")
    if url:
        props["Canvas URL"] = {"url": url}

    return props


def get_canvas_id(page):
    try:
        return page["properties"]["Canvas ID"]["rich_text"][0]["plain_text"]
    except (KeyError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def sync():
    print("Ensuring Notion DB schema is up to date...")
    ensure_db_properties()

    print("Fetching active Canvas courses...")
    courses = canvas_get("/courses", params={
        "enrollment_state": "active",
        "enrollment_type[]": "student",
        "state[]": "available",
        "per_page": 100,
    })
    print(f"  {len(courses)} courses found")

    all_assignments = []
    for course in courses:
        cid   = course["id"]
        cname = course.get("name") or course.get("course_code") or f"Course {cid}"
        print(f"  Syncing: {cname}")
        try:
            assignments = canvas_get(
                f"/courses/{cid}/assignments",
                params={
                    "include[]": "submission",
                    "per_page": 100,
                    "order_by": "due_at",
                },
            )
            for a in assignments:
                a["_course_name"] = cname
            all_assignments.extend(assignments)
        except requests.HTTPError as e:
            print(f"    Skipped ({e})")

    print(f"\n{len(all_assignments)} total assignments from Canvas")

    print("Loading existing Notion pages...")
    existing_pages = notion_query_all()
    by_canvas_id   = {get_canvas_id(p): p for p in existing_pages if get_canvas_id(p)}
    print(f"  {len(by_canvas_id)} already synced")

    created = updated = errors = 0
    for asgn in all_assignments:
        canvas_id = str(asgn["id"])
        props     = build_properties(asgn, asgn["_course_name"])
        try:
            if canvas_id in by_canvas_id:
                # Never overwrite the user's Status — only update everything else
                notion_update(by_canvas_id[canvas_id]["id"], props)
                updated += 1
            else:
                props["Status"] = {"status": {"name": "Not started"}}
                notion_create(props)
                created += 1
        except requests.HTTPError as e:
            print(f"  Error on '{asgn['name']}': {e}")
            errors += 1

    print(f"\nDone — {created} created, {updated} updated, {errors} errors")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    sync()
