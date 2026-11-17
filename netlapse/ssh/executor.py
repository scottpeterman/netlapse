"""
Netlapse SSH Executor — per-device command collection engine.

Path: netlapse/ssh/executor.py

The bridge between DCIM device records and SSH collection. Takes a
v_device_detail row (from NetlapseDB), builds an SSHClientConfig from
platform fields, connects, runs the job's command list, and returns
structured results.

The executor is synchronous (Paramiko is blocking). The scheduler
runs it in a ThreadPoolExecutor from its async event loop.

Design:
  - Does NOT touch storage or the database — returns results only.
    The scheduler handles persistence (storage.store_batch(),
    db.update_device_collection_status()).
  - Returns Snapshot objects ready for storage.store_batch().
  - Error categorization from SC2/VelocityCollector lineage.
  - Callback for progress reporting (feeds WebSocket via scheduler).

Usage:
    from netlapse.ssh.executor import collect_device

    device = db.get_device_by_id(42)
    result = collect_device(
        device=device,
        commands=["show running-config"],
        capture_type="config",
        credentials=("admin", "secret"),
    )

    if result.success:
        storage.store_batch(result.snapshots, trigger="scheduled")
"""

import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Callable, Tuple, Union

from .client import SSHClient, SSHClientConfig
from ..storage.backend import Snapshot

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Error categories — carried from SC2/VelocityCollector lineage
# ═══════════════════════════════════════════════════════════════════════════

class ErrorCategory:
    """Standardized error categories for job_device_results."""
    CONNECTION_REFUSED = "connection_refused"
    CONNECTION_TIMEOUT = "connection_timeout"
    AUTH_FAILURE = "auth_failure"
    HOST_UNREACHABLE = "host_unreachable"
    DNS_FAILURE = "dns_failure"
    SSH_PROTOCOL = "ssh_protocol"
    SHELL_TIMEOUT = "shell_timeout"
    PROMPT_DETECTION = "prompt_detection"
    COMMAND_TIMEOUT = "command_timeout"
    COMMAND_ERROR = "command_error"
    EMULATION_MISS = "emulation_miss"
    UNKNOWN = "unknown"


def categorize_error(error: Exception) -> Tuple[str, str]:
    """
    Categorize an SSH/collection error for structured reporting.

    Returns:
        (category, message) tuple.
    """
    msg = str(error).lower()
    full_msg = str(error)

    if "connection refused" in msg:
        return ErrorCategory.CONNECTION_REFUSED, full_msg
    # paramiko's NoValidConnectionsError ("Unable to connect to port 22 on
    # 10.0.0.1") — the refused/RST case, which otherwise fell through to UNKNOWN.
    if "unable to connect to port" in msg:
        return ErrorCategory.CONNECTION_REFUSED, full_msg
    if "timed out" in msg or "timeout" in msg:
        if "connect" in msg or "socket" in msg:
            return ErrorCategory.CONNECTION_TIMEOUT, full_msg
        return ErrorCategory.SHELL_TIMEOUT, full_msg
    if "authentication" in msg or "auth" in msg or "permission denied" in msg:
        return ErrorCategory.AUTH_FAILURE, full_msg
    if "no route to host" in msg or "unreachable" in msg:
        return ErrorCategory.HOST_UNREACHABLE, full_msg
    if "name or service not known" in msg or "nodename nor servname" in msg:
        return ErrorCategory.DNS_FAILURE, full_msg
    if "incompatible" in msg or "no matching" in msg or "kex" in msg:
        return ErrorCategory.SSH_PROTOCOL, full_msg
    if "not connected" in msg:
        return ErrorCategory.CONNECTION_REFUSED, full_msg
    if "emulation" in msg and "unresolved" in msg:
        return ErrorCategory.EMULATION_MISS, full_msg

    return ErrorCategory.UNKNOWN, full_msg


# ═══════════════════════════════════════════════════════════════════════════
# Result types
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CommandResult:
    """Result of a single command execution."""
    command: str
    raw_output: str
    success: bool
    duration_seconds: float
    error: Optional[str] = None


@dataclass
class DeviceResult:
    """
    Complete result of collecting one device.

    Contains Snapshot objects ready for storage.store_batch(),
    plus metadata for job_device_results and progress reporting.
    """
    device_id: int
    device_name: str
    site_slug: str
    success: bool
    snapshots: List[Snapshot] = field(default_factory=list)
    command_results: List[CommandResult] = field(default_factory=list)
    hostname: Optional[str] = None      # From prompt detection
    duration_seconds: float = 0.0
    error_category: Optional[str] = None
    error_message: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# Host resolution
# ═══════════════════════════════════════════════════════════════════════════

# RFC 6598 shared address space. A device whose management address is in this
# range is typically reachable only from inside the carrier's NAT, so the
# address stored in DCIM is not dialable from the collector.
CGNAT_DEFAULT = "100.64.0.0/10"

# name -> (address or None, expires_at). Collection runs every capture against
# every device, so an uncached lookup per connection would multiply DNS load by
# the number of jobs. Negative results are cached too — a name that does not
# resolve should not be retried on every capture in the same cycle.
_dns_cache: Dict[str, Tuple[Optional[str], float]] = {}


def clear_dns_cache() -> None:
    """Drop cached lookups. Called by tests; safe to call at any time."""
    _dns_cache.clear()


def _lookup(name: str, ttl: float) -> Optional[str]:
    now = time.time()
    hit = _dns_cache.get(name)
    if hit is not None and hit[1] > now:
        return hit[0]
    try:
        addr = socket.gethostbyname(name)
    except (socket.gaierror, OSError):
        addr = None
    _dns_cache[name] = (addr, now + ttl)
    return addr


def resolve_ssh_host(
    device: Dict[str, Any],
    opts: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], str]:
    """
    Decide which host string to dial for a device. Returns (host, note).

    Default behavior is unchanged from before this existed: primary_ip4, then
    oob_ip. Everything below is opt-in through the `collection:` section.

        collection:
          resolve_cgnat: true              # DNS when primary_ip4 is CGNAT
          cgnat_network: 100.64.0.0/10     # override the range if yours differs
          resolve_domain: lab.example.com  # appended when the bare name misses
          resolve_missing_address: true    # DNS when there is no address at all
          dns_cache_seconds: 300

    `resolve_domain` matters more than it looks. Devices imported from a
    topology map are stored as {shortname}.{site} with the domain stripped, so
    the stored name is usually not resolvable on its own — the bare name is
    tried first, then name.domain.

    A lookup that returns another CGNAT address is treated as a miss: DNS is
    agreeing with what DCIM already had, which is not reachable either. The
    fallback order after that is oob_ip, then the original address, so a failed
    resolution degrades to exactly the previous behavior rather than to no host.
    """
    opts = opts or {}
    addr = (device.get("primary_ip4") or "").strip() or None
    oob = (device.get("oob_ip") or "").strip() or None
    name = (device.get("name") or "").strip() or None

    try:
        cgnat_net = ipaddress.ip_network(
            opts.get("cgnat_network") or CGNAT_DEFAULT, strict=False
        )
    except ValueError:
        logger.warning(
            "collection.cgnat_network is not a valid network; using %s",
            CGNAT_DEFAULT,
        )
        cgnat_net = ipaddress.ip_network(CGNAT_DEFAULT)

    def _is_cgnat(value: Optional[str]) -> bool:
        if not value:
            return False
        try:
            parsed = ipaddress.ip_address(value)
        except ValueError:
            return False
        return parsed.version == 4 and parsed in cgnat_net

    want_dns = False
    reason = ""
    if addr and opts.get("resolve_cgnat") and _is_cgnat(addr):
        want_dns, reason = True, f"{addr} is CGNAT"
    elif not addr and not oob and opts.get("resolve_missing_address"):
        want_dns, reason = True, "no address on record"

    if want_dns and name:
        ttl = float(opts.get("dns_cache_seconds") or 300)
        domain = (opts.get("resolve_domain") or "").strip().lstrip(".")
        candidates = [name]
        if domain and not name.lower().endswith("." + domain.lower()):
            candidates.append(f"{name}.{domain}")

        for candidate in candidates:
            resolved = _lookup(candidate, ttl)
            if resolved and not _is_cgnat(resolved):
                return resolved, f"{reason}; resolved {candidate} -> {resolved}"
            if resolved:
                logger.debug(
                    "DNS for %s returned %s, also CGNAT — ignoring",
                    candidate, resolved,
                )
        if oob:
            return oob, f"{reason}; DNS missed, using oob_ip"
        if addr:
            return addr, f"{reason}; DNS missed, keeping stored address"

    return addr or oob, ""


# ═══════════════════════════════════════════════════════════════════════════
# Device → SSHClientConfig mapping
# ═══════════════════════════════════════════════════════════════════════════

def build_ssh_config(
    device: Dict[str, Any],
    credentials: Union[Tuple[str, str], Tuple[str, str, str]],
    timeouts: Optional[Dict[str, Any]] = None,
    jump: Optional[Any] = None,
) -> SSHClientConfig:
    """
    Build SSHClientConfig from a v_device_detail row + credentials.

    The v_device_detail view joins device, site, platform, manufacturer,
    role, and credential tables. All platform-specific SSH behavior
    (paging, prompt regex, enable, legacy algorithms) flows through
    the config dataclass into the SSH client.

    Args:
        device: Row dict from v_device_detail.
        credentials: (username, password) or (username, password, key_content).
        timeouts: Resolved `collection:` section from config.yaml. Absent or
            empty falls back to the documented defaults.

    Returns:
        Configured SSHClientConfig.
    """
    t = timeouts or {}
    host, note = resolve_ssh_host(device, t)
    if not host:
        raise ValueError(
            f"Device {device['name']} has no primary_ip4 or oob_ip"
        )
    if note:
        logger.info("Device %s: %s", device.get("name"), note)

    username = credentials[0]
    password = credentials[1] if len(credentials) > 1 else None
    key_content = credentials[2] if len(credentials) > 2 else None

    return SSHClientConfig(
        host=host,
        username=username,
        password=password,
        key_content=key_content,
        port=device.get("ssh_port") or 22,
        # `or default` rather than dict.get's default: a key present but null
        # in YAML parses to None, which get() would pass straight through.
        timeout=t.get("connect_timeout") or 90,
        expect_prompt_timeout=t.get("expect_prompt_timeout") or 3000,
        shell_timeout=t.get("shell_timeout") or 5.0,
        echo_grace=t.get("echo_grace") or 1.5,
        hard_deadline_multiplier=t.get("hard_deadline_multiplier") or 20,
        # 0.0 is a legitimate setting (no inter-command pause), so this one
        # needs an explicit None test — `or` would promote it back to 1.0.
        inter_command_time=(
            t["inter_command_time"]
            if t.get("inter_command_time") is not None else 1.0
        ),
        # Platform-specific SSH behavior from dcim_platform
        paging_disable_command=device.get("paging_disable_command"),
        prompt_regex=device.get("prompt_regex"),
        enable_command=device.get("enable_command"),
        # Device-level legacy_ssh overrides platform default.
        # device_legacy_ssh: NULL = use platform, 0 = force off, 1 = force on
        # legacy_ssh: platform-level default from dcim_platform
        legacy_ssh=_resolve_legacy_ssh(device),
        # Jump host / bastion spec, resolved per-device by ProxyResolver.
        jump=jump,
    )


def _resolve_legacy_ssh(device: Dict[str, Any]) -> bool:
    """
    Resolve effective legacy SSH setting for a device.

    Priority: device override > platform default > False.
    device_legacy_ssh is NULL (platform default), 0 (force off), or 1 (force on).
    """
    device_override = device.get("device_legacy_ssh")
    if device_override is not None:
        return bool(device_override)
    return bool(device.get("legacy_ssh"))


# ═══════════════════════════════════════════════════════════════════════════
# Single-device collection
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_credentials(
    credentials: Any,
) -> List[Tuple[str, Tuple]]:
    """
    Normalize the various accepted credential shapes into [(label, cred_tuple)].

    Accepts:
      - a single (username, password[, key]) tuple   → [("default", cred)]
      - a list of bare cred tuples                    → [("cred[0]", c0), ...]
      - a list of (label, cred_tuple) pairs           → used as-is

    A 2-tuple is a labeled pair only when its second element is itself a
    tuple; otherwise it's a bare (username, password) credential.
    """
    if isinstance(credentials, tuple):
        return [("default", credentials)]

    if isinstance(credentials, list):
        out: List[Tuple[str, Tuple]] = []
        for i, item in enumerate(credentials):
            if (
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[1], tuple)
            ):
                out.append((str(item[0]), item[1]))      # (label, cred)
            elif isinstance(item, tuple):
                out.append((f"cred[{i}]", item))          # bare cred
            else:
                raise ValueError(
                    f"Invalid credential entry at index {i}: {item!r}"
                )
        return out

    raise ValueError(
        f"credentials must be a tuple or list, got {type(credentials).__name__}"
    )


def collect_device(
    device: Dict[str, Any],
    commands: List[str],
    capture_type: str,
    credentials: Any,
    timeouts: Optional[Dict[str, Any]] = None,
    progress_callback: Optional[Callable[[str, str], None]] = None,
    jump: Optional[Any] = None,
    command_capture_types: Optional[List[str]] = None,
) -> DeviceResult:
    """
    Collect CLI output from a single device, trying an ordered credential chain.

    `credentials` may be a single (username, password[, key]) tuple (the
    original single-credential behavior), a list of such tuples, or a list
    of (label, cred_tuple) pairs.

    Credentials are tried in order. The chain advances to the next credential
    ONLY on an authentication failure. Every other outcome stops immediately:
      - success                              → return it
      - auth succeeded but commands failed   → return it (auth is not the problem)
      - connection timeout / refused / DNS / unreachable → return it

    Advancing on non-auth errors is deliberately avoided: it would waste
    attempts against an unreachable box and, worse, risk AAA (TACACS+/RADIUS)
    account lockout from spraying credentials. The chain is operator-defined
    and ordered (config.yaml `credentials.fallback_order`), so legacy gear
    that needs a different login is handled without blind spraying.

    Args:
        device: Row dict from v_device_detail.
        commands: List of CLI commands to execute.
        capture_type: Capture type label.
        credentials: Single credential tuple or an ordered credential chain.
        timeouts: Resolved `collection:` section from config.yaml.
        progress_callback: Optional (device_name, status_message) callback.
        command_capture_types: Optional list parallel to `commands` giving
            each command's own capture_type. Lets one SSH session collect
            several capture types (one login instead of one per capture).
            None = every snapshot is labeled `capture_type`.

    Returns:
        DeviceResult — the first success, or the last auth failure if every
        credential in the chain was rejected.
    """
    chain = _normalize_credentials(credentials)
    if not chain:
        raise ValueError("collect_device requires at least one credential")
    if command_capture_types is not None and len(command_capture_types) != len(commands):
        raise ValueError(
            "command_capture_types must be the same length as commands"
        )

    device_name = device.get("name", "?")
    last: Optional[DeviceResult] = None

    for idx, (label, cred) in enumerate(chain, 1):
        if len(chain) > 1:
            logger.debug(
                "%s: credential attempt %d/%d (%s)",
                device_name, idx, len(chain), label,
            )

        result = _attempt_collect(
            device=device,
            commands=commands,
            capture_type=capture_type,
            credentials=cred,
            timeouts=timeouts,
            progress_callback=progress_callback,
            cred_label=label,
            jump=jump,
            command_capture_types=command_capture_types,
        )

        if result.success:
            return result

        last = result

        # Only an authentication failure justifies trying the next credential.
        if result.error_category != ErrorCategory.AUTH_FAILURE:
            return result

        if idx < len(chain):
            logger.info(
                "%s: auth failed with credential %r — trying next in chain",
                device_name, label,
            )

    logger.warning(
        "%s: all %d credential(s) in the chain failed authentication",
        device_name, len(chain),
    )
    return last  # last is the final auth_failure DeviceResult


def _attempt_collect(
    device: Dict[str, Any],
    commands: List[str],
    capture_type: str,
    credentials: Union[Tuple[str, str], Tuple[str, str, str]],
    timeouts: Optional[Dict[str, Any]] = None,
    progress_callback: Optional[Callable[[str, str], None]] = None,
    cred_label: str = "default",
    jump: Optional[Any] = None,
    command_capture_types: Optional[List[str]] = None,
) -> DeviceResult:
    """
    Single-credential collection attempt against one device.

    Connects via SSH, detects prompt, disables pagination, optionally
    enters enable mode, runs each command, and builds Snapshot objects.
    This is the inner attempt; collect_device() wraps it with the
    ordered credential chain and auth-failure-only retry.

    Args:
        device: Row dict from v_device_detail.
        commands: List of CLI commands to execute.
        capture_type: Capture type label (config, arp, bgp, etc.).
        credentials: (username, password) or (username, password, key_content).
        timeouts: Resolved `collection:` section from config.yaml.
        progress_callback: Optional (device_name, status_message) callback.
        cred_label: Human label for the credential being tried (for logs).
        command_capture_types: Optional per-command capture_type labels
            (parallel to `commands`); see collect_device().

    Returns:
        DeviceResult with snapshots ready for storage.store_batch().
    """
    device_name = device["name"]
    site_slug = device.get("site_slug", "unknown")
    device_id = device.get("id", 0)
    start_time = time.time()

    def _progress(msg: str):
        if progress_callback:
            progress_callback(device_name, msg)

    _progress("connecting")

    # ── Build config from DCIM ───────────────────────────────────────
    try:
        config = build_ssh_config(device, credentials, timeouts, jump=jump)
    except ValueError as e:
        return DeviceResult(
            device_id=device_id,
            device_name=device_name,
            site_slug=site_slug,
            success=False,
            error_category=ErrorCategory.CONNECTION_REFUSED,
            error_message=str(e),
            duration_seconds=time.time() - start_time,
        )

    # ── Connect and collect ──────────────────────────────────────────
    snapshots: List[Snapshot] = []
    command_results: List[CommandResult] = []
    hostname: Optional[str] = None

    try:
        with SSHClient(config) as client:
            # Prompt detection
            _progress("detecting prompt")
            prompt = client.find_prompt()
            client.set_expect_prompt(prompt)
            hostname = client.extract_hostname_from_prompt()
            logger.info(
                "Connected to %s — prompt=%r hostname=%r emulated=%s cred=%r",
                device_name, prompt, hostname, client.is_emulated, cred_label,
            )

            # Enter enable mode if platform requires it
            if device.get("enable_command"):
                _progress("entering enable mode")
                client.send_enable()

            # Disable pagination
            _progress("disabling pagination")
            client.disable_pagination()

            # Execute commands
            for i, command in enumerate(commands, 1):
                cmd_label = f"command {i}/{len(commands)}"
                _progress(f"running {cmd_label}: {command}")

                cmd_start = time.time()
                try:
                    raw_output = client.execute_command(command)
                    cmd_duration = time.time() - cmd_start

                    command_results.append(CommandResult(
                        command=command,
                        raw_output=raw_output,
                        success=True,
                        duration_seconds=cmd_duration,
                    ))

                    # Build Snapshot for storage
                    now = datetime.now(timezone.utc)
                    snapshot = Snapshot(
                        device=device_name,
                        site=site_slug,
                        capture_type=(
                            command_capture_types[i - 1]
                            if command_capture_types else capture_type
                        ),
                        command=command,
                        raw_text=raw_output,
                        parsed_data=None,   # tfsm-fire wires in Phase 2
                        collected_at=now,
                    )
                    snapshots.append(snapshot)

                    logger.info(
                        "  %s: %s — %d bytes in %.1fs",
                        device_name, command, len(raw_output), cmd_duration,
                    )

                except Exception as e:
                    cmd_duration = time.time() - cmd_start
                    cat, msg = categorize_error(e)
                    command_results.append(CommandResult(
                        command=command,
                        raw_output="",
                        success=False,
                        duration_seconds=cmd_duration,
                        error=msg,
                    ))
                    logger.warning(
                        "  %s: %s — FAILED (%s): %s",
                        device_name, command, cat, msg,
                    )

        duration = time.time() - start_time
        success_count = sum(1 for cr in command_results if cr.success)
        _progress(f"done — {success_count}/{len(commands)} commands")

        return DeviceResult(
            device_id=device_id,
            device_name=device_name,
            site_slug=site_slug,
            success=success_count > 0,
            snapshots=snapshots,
            command_results=command_results,
            hostname=hostname,
            duration_seconds=duration,
        )

    except Exception as e:
        duration = time.time() - start_time
        cat, msg = categorize_error(e)
        _progress(f"failed — {cat}")
        logger.error(
            "%s: collection failed (%s): %s [%.1fs]",
            device_name, cat, msg, duration,
        )

        return DeviceResult(
            device_id=device_id,
            device_name=device_name,
            site_slug=site_slug,
            success=False,
            duration_seconds=duration,
            error_category=cat,
            error_message=msg,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Batch collection (synchronous — scheduler wraps in thread pool)
# ═══════════════════════════════════════════════════════════════════════════

def collect_devices(
    devices: List[Dict[str, Any]],
    commands: List[str],
    capture_type: str,
    credentials: Union[Tuple[str, str], Tuple[str, str, str]],
    timeouts: Optional[Dict[str, Any]] = None,
    progress_callback: Optional[Callable[[str, str], None]] = None,
    max_failures: int = 0,
    proxy_resolver: Optional[Any] = None,
    command_map: Optional[Any] = None,
) -> List[DeviceResult]:
    """
    Collect from multiple devices sequentially.

    For parallel collection, the scheduler should use a ThreadPoolExecutor
    and call collect_device() per device. This function is for simple
    sequential use cases (CLI one-shot, testing).

    Args:
        devices: List of v_device_detail row dicts.
        commands: CLI commands to run on each device (the per-platform default).
        capture_type: Capture type label.
        credentials: Shared credentials for all devices.
        timeouts: Resolved `collection:` section from config.yaml.
        progress_callback: Optional (device_name, status) callback.
        max_failures: Stop after N consecutive failures (0 = never stop).
        command_map: Optional per-platform command overrides ({platform_slug:
            [cmds]} dict or its JSON string). When given, each device's command
            list is resolved with the shared registry resolver, so this batch
            path honors platform dialects identically to the scheduler. None
            preserves the original behavior (every device gets `commands`).

    Returns:
        List of DeviceResult, one per device.
    """
    from netlapse.registry.resolver import resolve_commands

    results: List[DeviceResult] = []
    consecutive_failures = 0

    for i, device in enumerate(devices, 1):
        device_commands = resolve_commands(
            commands, command_map, device.get("platform_slug")
        )

        logger.info(
            "Collecting %d/%d: %s",
            i, len(devices), device["name"],
        )

        result = collect_device(
            device=device,
            commands=device_commands,
            capture_type=capture_type,
            credentials=credentials,
            timeouts=timeouts,
            progress_callback=progress_callback,
            jump=proxy_resolver.resolve(device) if proxy_resolver else None,
        )
        results.append(result)

        if result.success:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if max_failures and consecutive_failures >= max_failures:
                logger.warning(
                    "Stopping after %d consecutive failures", max_failures
                )
                break

    succeeded = sum(1 for r in results if r.success)
    failed = len(results) - succeeded
    logger.info(
        "Batch complete: %d/%d succeeded, %d failed",
        succeeded, len(devices), failed,
    )
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Auth test — connect, detect prompt, capture debug, disconnect
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class AuthTestResult:
    """Result of an SSH authentication test."""
    success: bool
    device_name: str
    duration_seconds: float
    # Success details
    prompt: Optional[str] = None
    hostname: Optional[str] = None
    ssh_banner: Optional[str] = None
    kex_algorithm: Optional[str] = None
    cipher: Optional[str] = None
    auth_method: Optional[str] = None
    # Failure details
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    # Debug log
    debug_log: List[str] = field(default_factory=list)


class _LogCapture(logging.Handler):
    """Temporary handler that captures log messages into a list."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: List[str] = []

    def emit(self, record):
        try:
            msg = self.format(record)
            self.records.append(msg)
        except Exception:
            pass


def test_device_auth(
    device: Dict[str, Any],
    credentials: Union[Tuple[str, str], Tuple[str, str, str]],
    timeout: int = 15,
    timeouts: Optional[Dict[str, Any]] = None,
) -> AuthTestResult:
    """
    Test SSH authentication to a device without running commands.

    Connects, detects prompt, captures transport negotiation details,
    and disconnects. Captures debug-level logs from paramiko and
    netlapse.ssh for the diagnostic panel.

    Args:
        device: Row dict from v_device_detail.
        credentials: (username, password) or (username, password, key_content).
        timeout: Connection timeout in seconds (probe only — this path does
            not run commands, so the read-loop tuning does not apply).
        timeouts: Resolved `collection:` section from config.yaml. Only the
            host-resolution keys are consulted here; pass it so the probe
            dials the same host collection would.

    Returns:
        AuthTestResult with success/failure, transport info, and debug log.
    """
    device_name = device["name"]
    start_time = time.time()

    # ── Set up debug log capture ─────────────────────────────────
    capture = _LogCapture()
    capture.setFormatter(logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    ))

    # Capture from paramiko and netlapse.ssh
    loggers_to_capture = [
        logging.getLogger("paramiko"),
        logging.getLogger("netlapse.ssh"),
    ]
    for lg in loggers_to_capture:
        lg.addHandler(capture)
        lg.setLevel(logging.DEBUG)

    try:
        # ── Build config ─────────────────────────────────────────
        # Merge the resolved `collection:` section so host resolution matches
        # what collection would do; the explicit probe timeout still wins.
        probe_opts = dict(timeouts or {})
        probe_opts["connect_timeout"] = timeout
        config = build_ssh_config(device, credentials, probe_opts)

        # Log what we're testing (sanitized)
        auth_type = "key" if len(credentials) > 2 else "password"
        capture.records.append(
            f"→ Testing {device_name} ({config.host}:{config.port}) "
            f"as {credentials[0]} via {auth_type}"
        )
        if config.legacy_mode:
            capture.records.append("→ Legacy SSH algorithms enabled")

        # ── Connect and probe ────────────────────────────────────
        with SSHClient(config) as client:
            # Prompt detection
            prompt = client.find_prompt()
            client.set_expect_prompt(prompt)
            hostname = client.extract_hostname_from_prompt()

            # Extract transport info
            ssh_banner = None
            kex_alg = None
            cipher = None
            transport = getattr(client, '_client', None)
            if transport:
                transport = transport.get_transport()
            if transport:
                ssh_banner = getattr(transport, 'remote_version', None)
                # Negotiated algorithms
                try:
                    sec = transport.get_security_options()
                    # Active (negotiated) values aren't in security_options,
                    # they're on the transport itself
                    pass
                except Exception:
                    pass
                # Paramiko stores negotiated info in transport fields
                kex_alg = getattr(transport, 'kex_engine', None)
                if kex_alg:
                    kex_alg = type(kex_alg).__name__
                local_cipher = getattr(transport, 'local_cipher', None)
                remote_cipher = getattr(transport, 'remote_cipher', None)
                if local_cipher:
                    cipher = local_cipher

            duration = time.time() - start_time

            capture.records.append(
                f"✓ Auth OK — prompt={prompt!r} hostname={hostname!r} "
                f"in {duration:.1f}s"
            )

            return AuthTestResult(
                success=True,
                device_name=device_name,
                duration_seconds=round(duration, 2),
                prompt=prompt,
                hostname=hostname,
                ssh_banner=ssh_banner,
                kex_algorithm=kex_alg,
                cipher=cipher,
                auth_method=auth_type,
                debug_log=list(capture.records),
            )

    except Exception as e:
        duration = time.time() - start_time
        cat, msg = categorize_error(e)

        capture.records.append(
            f"✗ Auth FAILED — {cat}: {msg} in {duration:.1f}s"
        )

        return AuthTestResult(
            success=False,
            device_name=device_name,
            duration_seconds=round(duration, 2),
            error_category=cat,
            error_message=msg,
            auth_method="key" if len(credentials) > 2 else "password",
            debug_log=list(capture.records),
        )

    finally:
        # Remove capture handlers
        for lg in loggers_to_capture:
            lg.removeHandler(capture)