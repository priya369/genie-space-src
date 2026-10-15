#!/usr/bin/env python3
"""
genie-deploy.py

Plan/apply Databricks Genie Spaces from explicitly selected YAML files
(the format written by pull_genie_space.py).

Each --config-file represents ONE Genie Space, with the target environment's real values already in it:

    title: Sales Analytics
    description: ...
    pull_version: 1                 # bookkeeping for the pull; ignored here
    warehouse_id: 84dd628b8e1cfd2c  # required
    parent_path: /Shared/genie      # optional -- omit it and Databricks picks the folder
    config: {...}
    data_sources: {...}
    instructions: {...}
    benchmarks: {...}
    shared_to:                      # optional
      - group: peacock-dbr-techops-team
        permission: CAN_MANAGE
      - group: peacock-dbr-engineering-team
        permission: CAN_EDIT

Behaviour
---------
- CREATE: file has no matching remote space yet -> create it.
- UPDATE: matching remote space exists and its content or permissions
  differ -> FULL REPLACE of description, warehouse_id, parent_path (if
  given) and serialized_space (config/data_sources/instructions/
  benchmarks). Removing a section, an instruction or a SQL sample from
  the YAML and re-applying REMOVES it from the live space too. The plan
  shows why: [content], [permissions] or both.
- DELETE: CI passes --delete-slug for YAML files explicitly deleted by the merge.
  Only the matching remotely managed Genie Space is considered for deletion;
  unchanged YAML files are never inferred as deletions.
- shared_to declared: the ACL is synced to exactly those user/group +
  permission pairs (a full replace of direct grants). Remove an entry and
  re-apply to revoke it. The service principal that owns the space, and
  inherited grants such as workspace admins, are not compared.
- shared_to absent: permissions are left exactly as they are.

Usage
-----
  python genie_space.py plan  --config-file genie-spaces/techops/dev/sales.yaml
  python genie_space.py apply --config-file genie-spaces/techops/dev/sales.yaml --delete-slug old-space --dry-run

Environment variables
----------------------
  DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET
      Target workspace + OAuth service-principal creds (client_credentials).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class DesiredSpace:
    slug: str                 # derived from filename, used for the managed marker
    file_path: Path
    title: str
    description: str
    warehouse_id: Any
    parent_path: Optional[str]      # None -> not sent, not compared
    config: Dict[str, Any]
    data_sources: Dict[str, Any]
    instructions: Dict[str, Any]
    benchmarks: Dict[str, Any]
    # None  = the YAML has no shared_to key -> permissions are left alone.
    # [...] = declared -> the ACL is synced to exactly this list.
    shared_to: Optional[List[Dict[str, Any]]] = None
    plan_reasons: List[str] = field(default_factory=list)   # why it is an update


@dataclass
class PlanResult:
    create: List[DesiredSpace] = field(default_factory=list)
    update: List[tuple[DesiredSpace, Dict[str, Any]]] = field(default_factory=list)  # (desired, current_remote)
    delete: List[Dict[str, Any]] = field(default_factory=list)  # remote space dicts
    unchanged: List[DesiredSpace] = field(default_factory=list)


# --------------------------------------------------------------------------
# Databricks client
# --------------------------------------------------------------------------

def _raise_with_body(resp: requests.Response, action: str) -> None:
    """requests' default HTTPError swallows the response body, which is
    where Databricks puts the actual error_code/message. Surface it."""
    if resp.ok:
        return
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    raise RuntimeError(f"{action} failed: HTTP {resp.status_code} — {body}")


class GenieClient:
    def __init__(self, host: str, token: str, timeout: int = 30) -> None:
        self.host = host.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.timeout = timeout

    def list_spaces(self) -> List[Dict[str, Any]]:
        resp = requests.get(f"{self.host}/api/2.0/genie/spaces", headers=self.headers, timeout=self.timeout)
        _raise_with_body(resp, "list_spaces")
        data = resp.json()
        for key in ("spaces", "items", "data", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        return []

    def get_space(self, space_id: str) -> Dict[str, Any]:
        resp = requests.get(
            f"{self.host}/api/2.0/genie/spaces/{space_id}",
            headers=self.headers,
            params={"include_serialized_space": "true"},
            timeout=self.timeout,
        )
        _raise_with_body(resp, f"get_space({space_id})")
        return resp.json()

    def create_space(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = requests.post(f"{self.host}/api/2.0/genie/spaces", headers=self.headers, json=payload, timeout=self.timeout)
        _raise_with_body(resp, f"create_space({payload.get('title')})")
        return resp.json()

    def update_space(self, space_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = requests.patch(
            f"{self.host}/api/2.0/genie/spaces/{space_id}", headers=self.headers, json=payload, timeout=self.timeout
        )
        _raise_with_body(resp, f"update_space({space_id})")
        return resp.json()

    def delete_space(self, space_id: str) -> None:
        resp = requests.delete(f"{self.host}/api/2.0/genie/spaces/{space_id}", headers=self.headers, timeout=self.timeout)
        _raise_with_body(resp, f"delete_space({space_id})")

    def get_permissions(self, space_id: str) -> Dict[str, Any]:
        # Genie Spaces use Databricks' generic Permissions API with object
        # type "genie" -- NOT a /genie/spaces/{id}/permissions sub-resource
        # (that path 404s: ENDPOINT_NOT_FOUND).
        resp = requests.get(
            f"{self.host}/api/2.0/permissions/genie/{space_id}", headers=self.headers, timeout=self.timeout
        )
        _raise_with_body(resp, f"get_permissions({space_id})")
        return resp.json()

    def set_permissions(self, space_id: str, acl: List[Dict[str, Any]]) -> Dict[str, Any]:
        # PUT fully replaces the ACL (matches this tool's full-replace model);
        # the generic Permissions API also has PATCH for a merge-update if
        # that's ever needed instead.
        resp = requests.put(
            f"{self.host}/api/2.0/permissions/genie/{space_id}",
            headers=self.headers,
            json={"access_control_list": acl},
            timeout=self.timeout,
        )
        _raise_with_body(resp, f"set_permissions({space_id})")
        return resp.json()


def get_oauth_token(host: str, client_id: str, client_secret: str, timeout: int) -> str:
    resp = requests.post(
        f"{host.rstrip('/')}/oidc/v1/token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials", "scope": "all-apis"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# --------------------------------------------------------------------------
# Helpers: slugs and managed marker
# --------------------------------------------------------------------------

def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    return re.sub(r"-+", "-", slug).strip("-")


def managed_marker(slug: str) -> str:
    return f"[genie-managed:{slug}]"


def extract_managed_slug(description: Optional[str]) -> Optional[str]:
    if not description:
        return None
    match = re.search(r"\[genie-managed:([a-z0-9-]+)\]", description.lower())
    return match.group(1) if match else None


def strip_marker(description: Optional[str]) -> str:
    if not description:
        return ""
    return re.sub(r"\s*\[genie-managed:[a-z0-9-]+\]\s*", " ", description, flags=re.IGNORECASE).strip()




# --------------------------------------------------------------------------
# Load desired state
# --------------------------------------------------------------------------

def load_desired_spaces(config_files: List[Path]) -> List[DesiredSpace]:
    spaces: List[DesiredSpace] = []
    for file_path in config_files:
        if not file_path.is_file():
            raise RuntimeError(f"Config file does not exist: {file_path}")
        if file_path.suffix.lower() not in {".yaml", ".yml"}:
            raise RuntimeError(f"Config file must be YAML: {file_path}")
        with file_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise RuntimeError(f"{file_path}: top-level YAML must be an object")


        title = str(raw.get("title") or "").strip()
        if not title:
            raise RuntimeError(f"{file_path}: missing required field 'title'")

        if not str(raw.get("warehouse_id") or "").strip():
            raise RuntimeError(f"{file_path}: missing required field 'warehouse_id'")

        spaces.append(
            DesiredSpace(
                slug=slugify(file_path.stem),
                file_path=file_path,
                title=title,
                description=str(raw.get("description") or ""),
                warehouse_id=raw.get("warehouse_id"),
                parent_path=str(raw["parent_path"]).strip() if raw.get("parent_path") else None,
                config=raw.get("config") or {},
                data_sources=raw.get("data_sources") or {},
                instructions=raw.get("instructions") or {},
                benchmarks=raw.get("benchmarks") or {},
                shared_to=raw.get("shared_to"),
            )
        )

    slugs_seen: Dict[str, Path] = {}
    for space in spaces:
        if space.slug in slugs_seen:
            raise RuntimeError(
                f"Duplicate managed slug '{space.slug}' from {space.file_path} "
                f"and {slugs_seen[space.slug]} — rename one of the files"
            )
        slugs_seen[space.slug] = space.file_path

    return spaces


# --------------------------------------------------------------------------
# Payload construction
# --------------------------------------------------------------------------

def build_serialized_space(desired: DesiredSpace) -> str:
    return json.dumps(
        {
            "version": 2,
            "config": desired.config,
            "data_sources": desired.data_sources,
            "instructions": desired.instructions,
            "benchmarks": desired.benchmarks,
        },
        sort_keys=True,
    )


def build_payload(desired: DesiredSpace) -> Dict[str, Any]:
    marker = managed_marker(desired.slug)
    description = strip_marker(desired.description)
    description = f"{description} {marker}".strip()

    payload = {
        "title": desired.title,
        "description": description,
        "warehouse_id": desired.warehouse_id,
        "serialized_space": build_serialized_space(desired),
    }
    if desired.parent_path:
        payload["parent_path"] = desired.parent_path
    return payload


VALID_PERMISSION_LEVELS = {"CAN_VIEW", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"}


def build_acl(desired: DesiredSpace) -> List[Dict[str, Any]]:
    acl: List[Dict[str, Any]] = []
    for entry in desired.shared_to or []:
        permission = entry.get("permission")
        if not permission:
            raise RuntimeError(f"{desired.file_path}: shared_to entry missing 'permission': {entry}")
        if permission not in VALID_PERMISSION_LEVELS:
            raise RuntimeError(
                f"{desired.file_path}: invalid permission '{permission}' — "
                f"must be one of {sorted(VALID_PERMISSION_LEVELS)}: {entry}"
            )
        item: Dict[str, Any] = {"permission_level": permission}
        if "user" in entry:
            item["user_name"] = entry["user"]
        elif "group" in entry:
            item["group_name"] = entry["group"]
        else:
            raise RuntimeError(f"{desired.file_path}: shared_to entry needs 'user' or 'group': {entry}")
        acl.append(item)
    return acl


# --------------------------------------------------------------------------
# Matching existing spaces
# --------------------------------------------------------------------------

def index_existing_by_slug(existing_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    indexed: Dict[str, Dict[str, Any]] = {}
    for item in existing_list:
        slug = extract_managed_slug(item.get("description"))
        if slug:
            indexed[slug] = item
    return indexed


def space_id_of(item: Dict[str, Any]) -> str:
    for key in ("space_id", "id", "name"):
        if item.get(key):
            return str(item[key])
    raise RuntimeError(f"Could not determine space id for remote item: {item}")


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------

def needs_update(desired_payload: Dict[str, Any], current_full: Dict[str, Any]) -> bool:
    current_desc = strip_marker(current_full.get("description"))
    desired_desc = strip_marker(desired_payload["description"])
    if current_desc != desired_desc:
        return True

    if str(current_full.get("warehouse_id") or "") != str(desired_payload.get("warehouse_id") or ""):
        return True
    # No parent_path in the YAML means "don't care where it lives" -- comparing
    # against whatever default folder Databricks chose would flag every run.
    if "parent_path" in desired_payload and str(current_full.get("parent_path") or "") != str(desired_payload["parent_path"]):
        return True

    current_serialized = current_full.get("serialized_space", "{}")
    if isinstance(current_serialized, dict):
        current_serialized_norm = json.dumps(current_serialized, sort_keys=True)
    else:
        try:
            current_serialized_norm = json.dumps(json.loads(current_serialized), sort_keys=True)
        except (TypeError, json.JSONDecodeError):
            current_serialized_norm = str(current_serialized)

    if current_serialized_norm != desired_payload["serialized_space"]:
        return True

    return False


# --------------------------------------------------------------------------
# Permission drift
# --------------------------------------------------------------------------

def _principal_key(entry: Dict[str, Any]) -> Optional[tuple]:
    for field_name in ("user_name", "group_name", "service_principal_name"):
        if entry.get(field_name):
            return (field_name, str(entry[field_name]))
    return None


def current_explicit_acl(permissions: Dict[str, Any]) -> Dict[tuple, str]:
    """Direct (non-inherited) grants to users and groups. Service principals
    and IS_OWNER are skipped: the owner is whoever created the space (this
    pipeline's own service principal), not something shared_to describes."""
    current: Dict[tuple, str] = {}
    for entry in permissions.get("access_control_list", []) or []:
        key = _principal_key(entry)
        if key is None or key[0] == "service_principal_name":
            continue
        grants = entry.get("all_permissions")
        if grants is None and entry.get("permission_level"):
            grants = [{"permission_level": entry["permission_level"]}]
        levels = [g.get("permission_level") for g in grants or [] if not g.get("inherited")]
        levels = [lvl for lvl in levels if lvl and lvl != "IS_OWNER"]
        if levels:
            current[key] = levels[0]
    return current


def acl_differs(client: "GenieClient", space_id: str, desired: DesiredSpace) -> bool:
    if desired.shared_to is None:
        return False
    try:
        current = current_explicit_acl(client.get_permissions(space_id))
    except RuntimeError as exc:
        print(f"WARN: cannot read permissions for {desired.title} ({exc}); not checking permission drift")
        return False
    wanted = {_principal_key(entry): entry["permission_level"] for entry in build_acl(desired)}
    return wanted != current


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------

def build_plan(client: GenieClient, desired_spaces: List[DesiredSpace], delete_slugs: List[str]) -> PlanResult:
    existing_list = client.list_spaces()
    existing_by_slug = index_existing_by_slug(existing_list)

    plan = PlanResult()
    for desired in desired_spaces:
        match = existing_by_slug.get(desired.slug)
        if match is None:
            plan.create.append(desired)
            continue

        current_full = client.get_space(space_id_of(match))
        payload = build_payload(desired)
        reasons = []
        if needs_update(payload, current_full):
            reasons.append("content")
        if acl_differs(client, space_id_of(match), desired):
            reasons.append("permissions")
        if reasons:
            desired.plan_reasons = reasons
            plan.update.append((desired, current_full))
        else:
            plan.unchanged.append(desired)

    # Delete only spaces explicitly requested by CI from deleted YAML files.
    for slug in delete_slugs:
        match = existing_by_slug.get(slug)
        if match is None:
            print(f"WARN: delete requested for '{slug}', but no managed remote Genie Space was found")
            continue
        plan.delete.append(match)

    return plan


def print_plan(plan: PlanResult) -> None:
    print("Genie Space Plan")
    print(f"  Create:    {len(plan.create)}")
    for d in plan.create:
        print(f"    + {d.title}  ({d.file_path.name})")
    print(f"  Update:    {len(plan.update)}")
    for d, _ in plan.update:
        print(f"    ~ {d.title}  ({d.file_path.name})  [{', '.join(d.plan_reasons)}]")
    print(f"  Delete:    {len(plan.delete)}")
    for item in plan.delete:
        print(f"    - {item.get('title')}  (space_id={space_id_of(item)})")
    print(f"  Unchanged: {len(plan.unchanged)}")


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------

def apply_plan(client: GenieClient, plan: PlanResult, dry_run: bool) -> None:
    for desired in plan.create:
        payload = build_payload(desired)
        print(f"CREATE {desired.title}")
        if dry_run:
            continue
        created = client.create_space(payload)
        space_id = space_id_of(created)
        acl = build_acl(desired)
        if acl:
            print(f"  PERMISSIONS {desired.title} -> {len(acl)} principal(s)")
            client.set_permissions(space_id, acl)

    for desired, current_full in plan.update:
        payload = build_payload(desired)
        space_id = space_id_of(current_full)
        print(f"UPDATE {desired.title} (space_id={space_id})")
        if dry_run:
            continue
        client.update_space(space_id, payload)
        if desired.shared_to is not None:
            acl = build_acl(desired)
            print(f"  PERMISSIONS {desired.title} -> {len(acl)} principal(s) (full replace)")
            client.set_permissions(space_id, acl)

    for item in plan.delete:
        space_id = space_id_of(item)
        print(f"DELETE {item.get('title')} (space_id={space_id})")
        if dry_run:
            continue
        client.delete_space(space_id)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan/apply Databricks Genie Spaces from YAML")
    parser.add_argument("mode", choices=["plan", "apply"])
    parser.add_argument(
        "--config-file",
        action="append",
        default=[],
        help="Added/modified Genie Space YAML file. Can be provided multiple times.",
    )
    parser.add_argument(
        "--delete-slug",
        action="append",
        default=[],
        help="Managed Genie Space slug from a deleted YAML filename. Can be provided multiple times.",
    )
    parser.add_argument("--host", default=os.environ.get("DATABRICKS_HOST", ""))
    parser.add_argument("--client-id", default=os.environ.get("DATABRICKS_CLIENT_ID", ""))
    parser.add_argument("--client-secret", default=os.environ.get("DATABRICKS_CLIENT_SECRET", ""))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=30)
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)

    if not args.host:
        raise RuntimeError("--host is required (or set DATABRICKS_HOST)")
    if not (args.client_id and args.client_secret):
        raise RuntimeError("--client-id/--client-secret are required (or set DATABRICKS_CLIENT_ID/SECRET)")

    token = get_oauth_token(args.host, args.client_id, args.client_secret, args.timeout)
    client = GenieClient(args.host, token, timeout=args.timeout)

    if not args.config_file and not args.delete_slug:
        print("No changed Genie Space YAML files to process.")
        return 0

    desired_spaces = load_desired_spaces([Path(file) for file in args.config_file])

    delete_slugs = [slugify(slug) for slug in args.delete_slug]
    desired_slugs = {space.slug for space in desired_spaces}
    overlap = desired_slugs.intersection(delete_slugs)
    if overlap:
        raise RuntimeError(f"Space(s) cannot be updated and deleted in the same run: {sorted(overlap)}")

    plan = build_plan(client, desired_spaces, delete_slugs)
    print_plan(plan)

    if args.mode == "apply":
        apply_plan(client, plan, dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # pylint: disable=broad-except
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
