# genie-space-src

# Genie Space Deployment

Manage Databricks **Genie Spaces** as code. Each space is a YAML file in this repo. A GitHub Actions pipeline checks the file (**plan**) and then creates or updates the space in Databricks (**apply**).

**In short:** author a space in the sandbox → pull it to YAML → open a PR → merge → the pipeline deploys it.

---

## 1. Repo layout

```
.github/
  workflows/
    genie-deploy.yaml     # plan + apply pipeline (dev / stage / prod)
    pr-checks.yaml        # lint + format checks on pull requests
  CODEOWNERS              # who must review changes
genie-space/
  dev/                    # one YAML per Genie Space for the dev workspace
    test.yaml
  stage/                  # same, for stage (add when ready)
  prod/                   # same, for prod (add when ready)
scripts/
  genie-pull.ipynb        # notebook: pull a space from Databricks into YAML
  genie-deploy.py         # plan / apply logic (calls the Genie API)
  genie-validate.sh       # entry point the pipeline runs
  requirements-genie.txt  # Python dependencies for the scripts
README.md
```

---

## 2. End-to-end flow

```
 Sandbox workspace            Your laptop / notebook          GitHub                    Databricks
 ┌──────────────────┐   1    ┌────────────────────┐   2    ┌──────────────┐   3     ┌──────────────┐
 │ Build the Genie  │ ─────► │ genie-pull.ipynb   │ ─────► │ PR with YAML │ ──────► │ plan → apply │
 │ Space by hand    │        │ saves <name>.yaml  │        │ in genie-    │         │ dev/stage/   │
 └──────────────────┘        └────────────────────┘        │ space/<env>/ │         │ prod         │
                                                           └──────────────┘         └──────────────┘
```

1. **Author** the space in the sandbox workspace using the Genie UI.
2. **Pull** it into a YAML file with `scripts/genie-pull.ipynb`.
3. **Commit** the YAML under `genie-space/<env>/` and open a PR. After merge, the pipeline deploys it.

---

## 3. The pipeline (`.github/workflows/genie-deploy.yaml`)

| Environment | Runs when | What it does |
|---|---|---|
| **dev** | push to `dev`, `main` or `release/**`, and `genie-space/dev/**` changed | plan, then apply |
| **stage** | push to `main` or `release/**`, and `genie-space/stage/**` changed | plan, then apply |
| **prod – plan** | push to `main` or `release/**`, and `genie-space/prod/**` changed | plan only (read-only) |
| **prod – apply** | **manual only** (Actions → *Run workflow* → tick *apply_prod*) | apply, after reviewer approval |

Plan always runs before apply, and a failed plan stops the apply.

### Pull request checks (`pr-checks.yaml`)
Every PR runs two checks in parallel: **ruff lint** and **ruff format**. Both must pass before merge.

### Secrets and variables

Set these per environment in **Settings → Environments** (`dev`, `stage`, `prod`):

| Name | Type | Meaning |
|---|---|---|
| `DATABRICKS_HOST` | Variable | Workspace URL, e.g. `https://<id>.gcp.databricks.com` |
| `DATABRICKS_CLIENT_ID` | Secret | Service principal client id |
| `DATABRICKS_CLIENT_SECRET` | Secret | Service principal client secret |

Add **required reviewers** on the `prod` environment. This is the approval gate for production applies.

---

## 4. Scripts

### `scripts/genie-validate.sh`
The pipeline entry point. It reads the settings below, installs nothing itself, and calls `genie-deploy.py` in the requested mode.

| Environment variable | Meaning |
|---|---|
| `GENIE_MODE` | `plan` (read-only) or `apply` (makes changes) |
| `GENIE_CONFIG_PATH` | Folder with the YAML files, e.g. `genie-space/dev` |
| `DATABRICKS_HOST` | Workspace URL |
| `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` | Service principal credentials |
| `GENIE_ALLOW_DELETE` | `"true"` lets apply delete things that were removed from YAML. Used on dev only. |

### `scripts/genie-deploy.py`
The core logic. It reads every YAML in `GENIE_CONFIG_PATH`, compares it with what exists in the workspace, and works out what to change. It talks to the Databricks **Genie REST API** using an OAuth token from the service principal.

**Plan mode** (safe, changes nothing). It prints what *would* happen:

| Symbol | Meaning |
|---|---|
| Create | the space is in YAML but not in the workspace |
| Update | the space exists but its content differs from YAML |
| Delete | the space or an item was removed from YAML (only reported if deletes are allowed) |
| No change | already matches |

**Apply mode** performs the same actions:

| Operation | What happens |
|---|---|
| **Create** | New space is created from the YAML. |
| **Read** | Existing spaces are fetched so they can be compared with YAML. |
| **Update** | Title, description, tables, instructions and benchmarks are patched to match YAML. |
| **Delete** | Removed items are deleted, only when `GENIE_ALLOW_DELETE` is `"true"`. |

> The YAML file is the single source of truth. Don't edit deployed spaces in the dev/stage/prod UI, because the next apply overwrites those edits.

### `scripts/genie-pull.ipynb`
A Databricks notebook that exports one Genie Space to YAML.

1. Clone this repo into Databricks (Git folder) and open the notebook in the **sandbox** workspace.
2. Set `SPACE_ID` in the config cell. The host and token come from your notebook session automatically.
3. Run all cells. The file is written **outside the repo**, to `/Workspace/Users/<you>/genie-export/<space_title>.yaml`, so it never ends up in git by accident.
4. Download or copy that file into `genie-space/<env>/` in the repo and open a PR.

You need at least *CAN VIEW* on the space you pull.

### `scripts/requirements-genie.txt`
Python packages used by the scripts. Install with `pip install -r scripts/requirements-genie.txt`.

---

## 5. YAML format

Each file describes one space:

```yaml
title: Sales Analytics
description: Ask questions about sales
warehouse_id: 84dd628b8e1cfd2c      # SQL warehouse the space runs on
config: {}                          # sample questions, etc.
data_sources: {}                    # tables the space can query
instructions: {}                    # text instructions, example SQL
benchmarks: {}                      # test questions
```

The **file name** (for example `test.yaml`) identifies the space across deployments. Rename a space's title freely, but don't rename the file, or it will be treated as a new space.

---

## 6. How to deploy a new or changed space

1. Build or edit the space in the **sandbox** workspace.
2. Run `genie-pull.ipynb` with the sandbox space id.
3. Create a branch, copy the YAML into `genie-space/dev/` and open a PR.
4. Check that the PR checks are green and get a review (see `CODEOWNERS`).
5. Merge. The pipeline runs **plan** then **apply** on dev.
6. To promote, copy the YAML to `genie-space/stage/` and then `genie-space/prod/`, adjusting `warehouse_id` and any catalog names for that environment.
7. For prod, start the **manual apply** from the Actions tab and wait for approval.

---

## 7. Troubleshooting

| Problem | Likely cause |
|---|---|
| Pull notebook returns HTTP 403/404 | Wrong `SPACE_ID`, or you lack permission on the space. |
| Plan fails with an auth error | Check the environment's `DATABRICKS_*` secrets and variable. |
| Pipeline didn't run | Your change wasn't under `genie-space/<env>/`, or the branch isn't `dev`, `main` or `release/**`. |
| Space was deleted unexpectedly | `GENIE_ALLOW_DELETE` is `"true"` and the item was removed from YAML. |
| PR check fails | Run `ruff check .` and `ruff format .` locally and fix the issues. |
