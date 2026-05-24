#!/usr/bin/env python3
"""Canvas LMS → Notion assignment sync."""

import os
import sys
import traceback
import requests

CANVAS_BASE    = "https://umassmed.instructure.com/api/v1"
CANVAS_TOKEN   = os.environ["CANVAS_TOKEN"].strip()
NOTION_TOKEN   = os.environ["NOTION_TOKEN"].strip()
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
        if not r.ok:
            print(f"  Canvas error {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            return data
        url = r.links.get("next", {}).get("url")
        params = None
    return results


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------

def notion_query_all():
    """Return all pages from the assignments DB."""
    results, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"https://api.notion.com/v1/databases/{ASSIGNMENTS_DB}/query",
            headers=NOTION_HDR, json=body, timeout=30,
        )
        if not r.ok:
            print(f"  Notion query error {r.status_code}: {r.text[:500]}")
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
    if not r.ok:
        print(f"  Notion create error {r.status_code}: {r.text[:500]}")
    r.raise_for_status()
    return r.json()


def notion_update(page_id, properties):
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=NOTION_HDR, json={"properties": properties}, timeout=30,
    )
    if not r.ok:
        print(f"  Notion update error {r.status_code}: {r.text[:500]}")
    r.raise_for_status()


def get_db_property_names():
    """Return the set of property names currently in the DB."""
    r = requests.get(
        f"https://api.notion.com/v1/databases/{ASSIGNMENTS_DB}",
        headers=NOTION_HDR, timeout=30,
    )
    if not r.ok:
        print(f"  Notion DB fetch error {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
    return set(r.json()["properties"].keys())


def ensure_db_properties(existing):
    """
    Add missing properties to the Notion DB schema.
    Non-fatal — if the integration lacks schema-edit permission we just
    work with whatever properties already exist.
    """
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
        return existing

    print(f"  Adding new DB properties: {list(to_add.keys())}")
    try:
        r = requests.patch(
            f"https://api.notion.com/v1/databases/{ASSIGNMENTS_DB}",
            headers=NOTION_HDR, json={"properties": to_add}, timeout=30,
        )
        if not r.ok:
            print(f"  Schema update failed ({r.status_code}): {r.text[:500]}")
            print("  Continuing with existing properties only.")
            return existing
        return existing | set(to_add.keys())
    except Exception as e:
        print(f"  Schema update exception: {e} — continuing anyway.")
        return existing


# ---------------------------------------------------------------------------
# Property builders
# ---------------------------------------------------------------------------

def _txt(val):
    return {"rich_text": [{"text": {"content": str(val)[:2000]}}]}

def _select(val):
    return {"select": {"name": str(val)[:100]}}

def _num(val):
    return {"number": float(val)} if val is not None else {"number": None}


def build_properties(asgn, course_name, known_props):
    """Build Notion property dict, skipping fields not in the DB schema."""
    sub       = asgn.get("submission") or {}
    wf        = sub.get("workflow_state", "unsubmitted")
    submitted = wf in ("submitted", "graded", "pending_review")
    graded    = wf == "graded"
    score     = sub.get("score")
    locked    = bool(asgn.get("locked_for_user", False))
    sub_types = asgn.get("submission_types") or []
    sub_type  = sub_types[0].replace("_", " ").title() if sub_types else "Assignment"

    due = asgn.get("due_at")
    url = asgn.get("html_url", "")

    candidates = {
        "Name":             {"title": [{"text": {"content": asgn["name"][:2000]}}]},
        "Due Date":         {"date": {"start": due[:10]}} if due else {"date": None},
        "Canvas ID":        _txt(asgn["id"]),
        "Course":           _select(course_name),
        "Points Possible":  _num(asgn.get("points_possible")),
        "Score":            _num(score),
        "Submitted":        {"checkbox": submitted},
        "Graded":           {"checkbox": graded},
        "Submission Type":  _select(sub_type),
        "Locked":           {"checkbox": locked},
    }
    if url:
        candidates["Canvas URL"] = {"url": url}

    # Only send properties the DB actually has
    return {k: v for k, v in candidates.items() if k in known_props}


def get_canvas_id(page):
    try:
        return page["properties"]["Canvas ID"]["rich_text"][0]["plain_text"]
    except (KeyError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def sync():
    print("Fetching Notion DB schema...")
    known_props = get_db_property_names()
    print(f"  Existing properties: {sorted(known_props)}")

    known_props = ensure_db_properties(known_props)

    print("\nFetching active Canvas courses...")
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
                params={"include[]": "submission", "per_page": 100, "order_by": "due_at"},
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
        props     = build_properties(asgn, asgn["_course_name"], known_props)
        try:
            if canvas_id in by_canvas_id:
                notion_update(by_canvas_id[canvas_id]["id"], props)
                updated += 1
            else:
                if "Status" in known_props:
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
    try:
        sync()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
