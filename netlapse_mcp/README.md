# netlapse_mcp

An MCP server that gives Claude (Claude Code in VS Code or the terminal) read access to
netlapse: inventory, parsed capture data, config search, history, semantic diffs and the
change feed. It can also trigger Collect Now, with a human approval on every call.

## What it can and can't do

| Can | Can't |
|---|---|
| Query **parsed records** with filters, field selection and paging | Download a whole capture (route table, config, anything) |
| Read **raw lines** of captures that have **no parse**, by regex or line range, capped | Read raw output of a capture that has parsed records |
| Search **config** by regex: fleet-wide, or one device with context and the enclosing stanza | Run any command: there is no SSH path in this server |
| Diff versions (semantic for parsed captures, unified for config) and run the fleet change feed | Touch backup/restore, the vault, users or config |
| Run **Collect Now** for the device's *job-defined* captures, after you approve the prompt | Send a command a job doesn't define |

netlapse enforces the same limits on the token itself (`auth.readonly_api_tokens`), so the
token can't do more than the tools even if it's used outside this server.

## Tools

| Tool | Purpose |
|---|---|
| `inventory_overview` | Device counts by site/role/platform, failing collections, capture types |
| `find_devices` | Search inventory by name/IP substring, site, role, platform, status |
| `device_summary` | One device: every capture, parsed or not, record counts, collected time |
| `get_records` | Parsed records with `where` filters (`F=v`, `F!=v`, `F~re`, `F!~re`), `fields`, `sort`, paging |
| `get_raw_lines` | Raw lines for **unparsed** captures only; pattern or line range; ≤ 200 lines |
| `search_config` | Config regex search; with `device=` adds context and parent stanza |
| `one_search` | Every place an IP / MAC / ASN / hostname / serial appears |
| `list_versions` | Stored versions for a device (and capture type) |
| `diff` | Version diff: semantic records or unified config diff |
| `recent_changes` | Fleet change feed for a window, with volatility filtered out |
| `collection_status` | Scheduler and job health |
| `collect_now` | Fresh collection of job-defined captures (**prompts every time**) |
| `collect_status` | Progress/outcome of a Collect Now request |

Devices can be named by full name (`edge1-01.site1`), short name (`edge1-01`), unique
substring, management IP, or id.

## Setup

**1. Create a token** on the netlapse host and add the printed snippet under `auth:` in
`config.yaml`, then restart netlapse. Drop `--collect` if this token should never trigger
collection.

```
python -m netlapse token --name mcp --collect
```

**2. Install the SDK** in the Python environment that will run the server (tested with
`mcp` 1.30 and 2.2):

```
pip install "mcp>=1.30"
```

**3. Register the server with Claude Code** (the VS Code extension uses the same config).
`PYTHONPATH` points at the directory that contains `netlapse_mcp/`. User scope makes it
available in every project:

```
claude mcp add --env NETLAPSE_URL=http://jumphost:8888 --env NETLAPSE_TOKEN=<token> --env PYTHONPATH=/path/to/netlapse-repo --transport stdio --scope user netlapse -- /path/to/.venv/bin/python -m netlapse_mcp
```

Or as a project `.mcp.json`. Claude Code asks once before using a project-scoped server.
`${VAR}` is expanded from your environment, which keeps the token out of the file:

```json
{
  "mcpServers": {
    "netlapse": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "netlapse_mcp"],
      "env": {
        "NETLAPSE_URL": "http://jumphost:8888",
        "NETLAPSE_TOKEN": "${NETLAPSE_MCP_TOKEN}",
        "PYTHONPATH": "/path/to/netlapse-repo"
      }
    }
  }
}
```

**4. Permissions.** Auto-approve the read tools, and make `collect_now` always ask. An
`ask` rule prompts even in auto mode, and it wins over a broader allow rule. Don't use
`mcp__netlapse__*` in `allow`: it would also cover `collect_now`. In `~/.claude/settings.json`
(or the project's `.claude/settings.json`):

```json
{
  "permissions": {
    "allow": [
      "mcp__netlapse__inventory_overview",
      "mcp__netlapse__find_devices",
      "mcp__netlapse__device_summary",
      "mcp__netlapse__get_records",
      "mcp__netlapse__get_raw_lines",
      "mcp__netlapse__search_config",
      "mcp__netlapse__one_search",
      "mcp__netlapse__list_versions",
      "mcp__netlapse__diff",
      "mcp__netlapse__recent_changes",
      "mcp__netlapse__collection_status",
      "mcp__netlapse__collect_status"
    ],
    "ask": [
      "mcp__netlapse__collect_now"
    ]
  }
}
```

`bypassPermissions` mode skips prompts. If a session might run that way, give this server a
token without `allow_collect`; netlapse then refuses Collect Now regardless of the client.

## Environment

| Variable | Default | |
|---|---|---|
| `NETLAPSE_URL` | `http://127.0.0.1:8888` | netlapse base URL |
| `NETLAPSE_TOKEN` | (required) | read-only token from `python -m netlapse token` |
| `NETLAPSE_TIMEOUT` | `60` | seconds per HTTP call |
| `NETLAPSE_VERIFY_TLS` | `1` | `0` to accept a self-signed cert on an https URL |

## Example questions

- "Which BGP sessions on the site1 edges aren't Established?"
- "What changed at site1 in the last 24 hours, and show me the BGP diff on anything that moved."
- "Where is 10.20.30.40 seen — ARP, MAC, routes, config?"
- "Which devices have `mtu 9214` configured on an interface toward an edge?"
- "Is the route to 203.0.113.0/24 the same on both tors? Collect fresh routes first if the
  data is older than an hour."
