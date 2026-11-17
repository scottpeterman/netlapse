"""
Allow running with `python -m netlapse`.

Subcommands:
    python -m netlapse                  # Start daemon (default)
    python -m netlapse serve            # Start daemon (explicit)
    python -m netlapse sync-map         # Import from SC2 topology map
    python -m netlapse vault init       # Initialize credential vault
    python -m netlapse vault add-ssh    # Add SSH credential
    python -m netlapse vault list       # List credentials
    python -m netlapse vault assign     # Assign credential to devices
    python -m netlapse collect          # Run collection cycle
    python -m netlapse collect --job config-backup  # Run a named job
    python -m netlapse fetch-templates  # Download the TextFSM template DB
    python -m netlapse backup           # Back up deployed state (config, DBs, definitions)
    python -m netlapse backup list      # List backups
    python -m netlapse restore ARCHIVE  # Stage a restore (applied now if daemon is stopped)
    python -m netlapse token --name mcp [--collect]   # New read-only API token + config snippet
"""
import sys
import os
import logging


def main():
    args = sys.argv[1:]

    # ── sync-map ─────────────────────────────────────────────────────
    if args and args[0] == "sync-map":
        from netlapse.dcim.map_importer import cli_sync_map
        sys.exit(cli_sync_map(args[1:]))

    # ── collect ──────────────────────────────────────────────────────
    if args and args[0] == "collect":
        from netlapse.core.collector import cli_collect
        sys.exit(cli_collect(args[1:]))

    # ── definitions ──────────────────────────────────────────────────
    if args and args[0] == "definitions":
        from netlapse.registry.cli import cli_definitions
        sys.exit(cli_definitions(args[1:]))

    # ── fetch-templates ──────────────────────────────────────────────
    if args and args[0] == "fetch-templates":
        sys.exit(_cli_fetch_templates(args[1:]))

    # ── vault ────────────────────────────────────────────────────────
    if args and args[0] == "vault":
        sys.exit(_cli_vault(args[1:]))

    # ── backup / restore ─────────────────────────────────────────────
    if args and args[0] == "backup":
        sys.exit(_cli_backup(args[1:]))
    if args and args[0] == "restore":
        sys.exit(_cli_restore(args[1:]))
    if args and args[0] == "token":
        sys.exit(_cli_token(args[1:]))

    # ── cleanup ──────────────────────────────────────────────────────
    if args and args[0] == "cleanup":
        from netlapse.storage.cleanup import cli_cleanup
        sys.exit(cli_cleanup(args[1:]))

    # ── serve (default) ──────────────────────────────────────────────
    if args and args[0] == "serve":
        args = args[1:]

    print(
        "http://localhost:8888/docs          # Swagger UI\n"
        "http://localhost:8888/nodes         # Oxidized compat\n"
        "http://localhost:8888/api/v1/health # Health check\n"
        "http://localhost:8888/api/v1/sites  # Site list"
    )

    from netlapse.app import main as start_server
    start_server()


def _cli_backup(args: list) -> int:
    """
    netlapse backup [--data none|current|all] [--label TEXT] [--out DIR]
    netlapse backup list

    Safe while the daemon runs (SQLite files are copied with the online
    backup API). Default is state only: config, definitions, netlapse.db,
    vault.db, tfsm_templates.db. --data current adds current snapshots;
    --data all adds _history (or the git repo).
    """
    import argparse
    from netlapse.backup import (
        BackupError, DATA_SCOPES, create_backup, list_backups,
        resolve_state_paths, _human,
    )

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    paths = resolve_state_paths()

    if args and args[0] == "list":
        rows = list_backups(paths.backups_dir)
        if not rows:
            print(f"No backups in {paths.backups_dir}")
            return 0
        print(f"{paths.backups_dir}")
        for r in rows:
            if r.get("error"):
                print(f"  {r['name']}  ({_human(r['size'])})  UNREADABLE: {r['error']}")
                continue
            print(f"  {r['name']}  {_human(r['size']):>9}  data={r.get('data_scope')}"
                  f"  v{r.get('netlapse_version')}  {r.get('host')}")
        return 0

    ap = argparse.ArgumentParser(prog="netlapse backup")
    ap.add_argument("--data", choices=DATA_SCOPES, default="none",
                    help="include collected snapshots (default: none)")
    ap.add_argument("--label", help="short tag added to the file name")
    ap.add_argument("--out", help=f"output directory (default: {paths.backups_dir})")
    ns = ap.parse_args(args)

    try:
        res = create_backup(paths, ns.data, label=ns.label, dest_dir=ns.out,
                            progress=lambda m: print(f"  {m}", flush=True))
    except BackupError as e:
        print(f"Backup failed: {e}")
        return 1
    m = res["manifest"]
    print(f"Wrote {res['path']}  ({_human(res['size'])})")
    print(f"  definitions: {', '.join(m['components']['definitions']) or 'none'}")
    print(f"  databases:   " + ", ".join(
        k for k in ("netlapse.db", "vault.db", "tfsm_templates.db") if m["components"].get(k)))
    if ns.data != "none":
        print(f"  data:        {m['data']['files']} files, {_human(m['data']['bytes'])} uncompressed")
    print("  Contains the vault (encrypted) and user password hashes — store it accordingly.")
    return 0


def _cli_token(args: list) -> int:
    """
    netlapse token --name NAME [--collect]

    Print a new read-only API token and the config.yaml snippet that enables
    it. Nothing is written — paste the snippet under `auth:` and restart.
    """
    import argparse
    import secrets
    ap = argparse.ArgumentParser(prog="netlapse token")
    ap.add_argument("--name", default="mcp")
    ap.add_argument("--collect", action="store_true",
                    help="allow this token to trigger Collect Now")
    ns = ap.parse_args(args)
    tok = secrets.token_urlsafe(32)
    print(f"Token for '{ns.name}':\n\n  {tok}\n")
    print("Add under `auth:` in config.yaml, then restart netlapse:\n")
    print("  readonly_api_tokens:")
    print(f"    - name: {ns.name}")
    print(f'      token: "{tok}"')
    print(f"      allow_collect: {'true' if ns.collect else 'false'}")
    return 0


def _cli_restore(args: list) -> int:
    """
    netlapse restore ARCHIVE      stage; applied immediately if no daemon runs,
                                  otherwise on the next daemon start
    netlapse restore --status     show pending / last restore
    netlapse restore --cancel     discard a staged restore

    ARCHIVE is a path or a file name in the backups directory.
    """
    import argparse
    import json
    from netlapse.backup import (
        BackupError, apply_pending_restore, cancel_restore, daemon_running,
        last_restore, pending_restore, resolve_archive, resolve_state_paths,
        stage_restore,
    )

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="netlapse restore")
    ap.add_argument("archive", nargs="?")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--cancel", action="store_true")
    ap.add_argument("--stage-only", action="store_true",
                    help="stage even if no daemon is running (apply on next start)")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    ns = ap.parse_args(args)

    paths = resolve_state_paths()
    cfg_dir = paths.config_dir
    from netlapse.backup import _load_yaml
    listen = _load_yaml(paths.config_path).get("listen") or {}

    if ns.cancel:
        print("Staged restore discarded." if cancel_restore(cfg_dir) else "Nothing staged.")
        return 0
    if ns.status or not ns.archive:
        p = pending_restore(cfg_dir)
        last = last_restore(cfg_dir)
        print(f"Daemon running: {'yes' if daemon_running(cfg_dir, listen) else 'no'}")
        print(f"Pending: {p['archive_name'] + ' (staged ' + p['staged_at'] + ')' if p and 'archive_name' in p else 'none'}")
        if last:
            print("Last restore:", json.dumps(last, indent=2))
        return 0

    try:
        archive = resolve_archive(paths.backups_dir, ns.archive)
        from netlapse.backup import read_manifest
        m = read_manifest(archive)
    except BackupError as e:
        print(f"Restore failed: {e}")
        return 1

    print(f"Archive:  {archive.name}")
    print(f"  created {m.get('created_at')} on {m.get('host')}, netlapse v{m.get('netlapse_version')}")
    print(f"  data scope: {m.get('data_scope')}"
          + ("  (current data directory will be REPLACED)" if m.get("data_scope") != "none"
             else "  (data directory left as-is)"))
    print("  Replaces config.yaml, definitions, netlapse.db (incl. users), vault.db"
          + (", tfsm_templates.db" if m.get("components", {}).get("tfsm_templates.db") else "")
          + ". A pre-restore backup of current state is taken first.")
    if not ns.yes:
        try:
            if input("Proceed? [y/N] ").strip().lower() != "y":
                print("Aborted.")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1

    try:
        pending = stage_restore(archive, paths, progress=lambda msg: print(f"  {msg}", flush=True))
    except BackupError as e:
        print(f"Restore failed: {e}")
        return 1
    if pending["vault_password"] == "mismatch":
        print("  WARNING: $NETLAPSE_VAULT_PASSWORD does not unlock the archived vault —"
              " collection will stay stopped until it is set to that vault's password.")

    if daemon_running(cfg_dir, listen) or ns.stage_only:
        print("Staged. Restart netlapse to apply it (e.g. docker restart <container>).")
        return 0

    res = apply_pending_restore(paths.config_path)
    if res and res.get("error"):
        print(f"Restore failed: {res['error']}")
        return 1
    print(f"Restored. Pre-restore backup: {res.get('pre_restore_backup', 'none')}")
    if res.get("previous_data_dir"):
        print(f"Previous data directory kept at {res['previous_data_dir']} — delete when satisfied.")
    return 0


def _cli_fetch_templates(args: list) -> int:
    """
    Download the TextFSM template DB that enables structured parsing.

        netlapse fetch-templates           # fetch if missing
        netlapse fetch-templates --force   # re-fetch even if present

    Resolves the destination from parser.db_path in config (default
    ~/.netlapse/tfsm_templates.db). Returns 0 on success, 1 otherwise.
    """
    import os
    from pathlib import Path
    from netlapse.app import fetch_template_db

    cfg = Path(
        os.environ.get("NETLAPSE_CONFIG", "~/.netlapse/config.yaml")
    ).expanduser()
    db_path = "~/.netlapse/tfsm_templates.db"
    if cfg.exists():
        import yaml
        c = yaml.safe_load(cfg.read_text()) or {}
        db_path = (c.get("parser") or {}).get("db_path") or db_path

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ok = fetch_template_db(Path(db_path).expanduser(), force="--force" in args)
    print("Template DB ready." if ok else "Template DB not fetched (see log above).")
    return 0 if ok else 1


def _cli_vault(args: list) -> int:
    """Vault subcommand dispatcher."""
    if not args:
        print("Usage: netlapse vault {init|add-ssh|delete|list|assign|unlock-test}")
        return 1

    subcmd = args[0]
    sub_args = args[1:]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if subcmd == "init":
        return _vault_init(sub_args)
    elif subcmd == "add-ssh":
        return _vault_add_ssh(sub_args)
    elif subcmd == "list":
        return _vault_list(sub_args)
    elif subcmd == "delete":
        return _vault_delete(sub_args)
    elif subcmd == "assign":
        return _vault_assign(sub_args)
    elif subcmd == "unlock-test":
        return _vault_unlock_test(sub_args)
    else:
        print(f"Unknown vault command: {subcmd}")
        return 1


def _vault_init(args: list) -> int:
    """Initialize the vault with a master password."""
    import getpass
    from netlapse.vault import CredentialVault

    vault = CredentialVault()

    if vault.is_initialized:
        print("Vault already initialized.")
        return 0

    password = os.environ.get("NETLAPSE_VAULT_PASSWORD")
    if not password:
        password = getpass.getpass("Master password (min 8 chars): ")
        confirm = getpass.getpass("Confirm: ")
        if password != confirm:
            print("Passwords don't match.")
            return 1

    try:
        vault.initialize(password)
        print(f"Vault initialized at {vault.db_path}")
        return 0
    except ValueError as e:
        print(f"Error: {e}")
        return 1


def _vault_add_ssh(args: list) -> int:
    """Add an SSH credential to the vault."""
    import argparse
    import getpass
    from netlapse.vault import CredentialVault
    from netlapse.vault.bridge import get_vault

    parser = argparse.ArgumentParser(prog="netlapse vault add-ssh")
    parser.add_argument("name", help="Credential name (unique)")
    parser.add_argument("--username", "-u", required=True, help="SSH username")
    parser.add_argument("--password", "-p", default=None,
                        help="SSH password (omit for key-only auth, prompted if no key given)")
    parser.add_argument("--key-file", "-k", default=None,
                        help="Path to SSH private key file (RSA, Ed25519, ECDSA)")
    parser.add_argument("--key-passphrase", default=None,
                        help="Passphrase for encrypted key (prompted if key is encrypted)")
    parser.add_argument("--default", action="store_true",
                        help="Set as default SSH credential")
    parser.add_argument("--description", "-d", default=None)
    parser.add_argument("--port", type=int, default=22)

    opts = parser.parse_args(args)

    vault = get_vault()
    if not vault.is_unlocked:
        print("Vault is locked. Set NETLAPSE_VAULT_PASSWORD or run: netlapse vault init")
        return 1

    # ── Load key file if provided ─────────────────────────────────
    key_content = None
    key_passphrase = opts.key_passphrase
    if opts.key_file:
        key_path = os.path.expanduser(opts.key_file)
        if not os.path.exists(key_path):
            print(f"Key file not found: {key_path}")
            return 1
        with open(key_path, "r") as f:
            key_content = f.read()
        print(f"Loaded key from {key_path} ({len(key_content)} bytes)")

    # ── Resolve password ──────────────────────────────────────────
    password = opts.password
    if not password and not key_content:
        # No key, no password flag — prompt for password
        password = getpass.getpass(f"SSH password for {opts.username}: ")
    elif not password and key_content:
        # Key-only auth — password stays None
        password = None

    try:
        cred_id = vault.add_ssh_credential(
            name=opts.name,
            username=opts.username,
            password=password,
            key_content=key_content,
            key_passphrase=key_passphrase,
            port=opts.port,
            description=opts.description,
            is_default=opts.default,
        )
        auth_methods = []
        if key_content:
            auth_methods.append("key")
        if password:
            auth_methods.append("password")
        print(f"Added SSH credential '{opts.name}' (id={cred_id}, auth={'+'.join(auth_methods)})")
        if opts.default:
            print("  (set as default)")
        return 0
    except Exception as e:
        print(f"Error: {e}")
        return 1


def _vault_list(args: list) -> int:
    """List credentials in the vault."""
    from netlapse.vault.bridge import get_vault

    vault = get_vault()
    if not vault.is_unlocked:
        print("Vault is locked. Set NETLAPSE_VAULT_PASSWORD.")
        return 1

    creds = vault.list_credentials()
    if not creds:
        print("No credentials stored.")
        return 0

    print(f"{'ID':>4}  {'Name':<20}  {'Type':<8}  {'Username':<15}  {'Auth':<12}  {'Default':<8}")
    print("-" * 75)
    for c in creds:
        default = "  *" if c.is_default else ""
        print(
            f"{c.id:>4}  {c.name:<20}  {c.type_display:<8}  "
            f"{c.display_username or '':<15}  {c.auth_summary:<12}  {default}"
        )
    return 0


def _vault_delete(args: list) -> int:
    """Delete a credential from the vault."""
    import argparse
    from netlapse.vault.bridge import get_vault

    parser = argparse.ArgumentParser(prog="netlapse vault delete")
    parser.add_argument("name", help="Credential name to delete")

    opts = parser.parse_args(args)

    vault = get_vault()
    if not vault.is_unlocked:
        print("Vault is locked. Set NETLAPSE_VAULT_PASSWORD.")
        return 1

    if vault.remove_credential(name=opts.name):
        print(f"Deleted credential '{opts.name}'")
        return 0
    else:
        print(f"Credential '{opts.name}' not found")
        return 1


def _vault_assign(args: list) -> int:
    """Assign a credential to devices."""
    import argparse
    from pathlib import Path
    from netlapse.dcim import NetlapseDB
    from netlapse.vault.bridge import get_vault, assign_credential_to_all

    parser = argparse.ArgumentParser(prog="netlapse vault assign")
    parser.add_argument("credential_name", help="Credential name from vault")
    parser.add_argument("--site", default=None, help="Filter by site slug")
    parser.add_argument("--role", default=None, help="Filter by role slug")
    parser.add_argument("--db", default=None, help="DCIM database path")

    opts = parser.parse_args(args)

    vault = get_vault()
    if not vault.is_unlocked:
        print("Vault is locked. Set NETLAPSE_VAULT_PASSWORD.")
        return 1

    db_path = Path(opts.db).expanduser() if opts.db else None
    db = NetlapseDB(db_path)

    try:
        count = assign_credential_to_all(
            db=db,
            vault=vault,
            credential_name=opts.credential_name,
            site_filter=opts.site,
            role_filter=opts.role,
        )
        print(f"Assigned '{opts.credential_name}' to {count} device(s)")
        return 0
    except Exception as e:
        print(f"Error: {e}")
        return 1
    finally:
        db.close()


def _vault_unlock_test(args: list) -> int:
    """Test vault unlock (verify password)."""
    from netlapse.vault.bridge import get_vault

    vault = get_vault()
    if vault.is_unlocked:
        print(f"Vault is unlocked ({vault.db_path})")
        return 0
    else:
        print("Vault is locked. Check NETLAPSE_VAULT_PASSWORD.")
        return 1


if __name__ == "__main__":
    main()