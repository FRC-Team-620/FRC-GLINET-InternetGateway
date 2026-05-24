#!/usr/bin/env python3
"""
FRC Robot Router Configurator for GL-iNet routers.

Configures a GL-iNet router as an internet bridge for an FRC robot following
the 10.TE.AM.x addressing scheme defined in the FRC Game Manual.

Static IP reservations (do not assign via DHCP):
  10.TE.AM.1  - VH-109 Radio (programmed by field kiosk)
  10.TE.AM.2  - roboRIO
  10.TE.AM.3  - Field network reserved
  10.TE.AM.4  - This router / field network reserved
  10.TE.AM.5  - Driver Station (when using static IP)
  10.TE.AM.6–.19  - Buffer for other static devices

DHCP range starts at .20 to avoid all reserved/static addresses.

DS WiFi block (enabled by default):
  Drops DS→Robot control packets forwarded from wlan0/wlan1 so that a laptop
  on WiFi cannot accidentally drive the robot. The DS must connect via Ethernet.
  Ports blocked from wlan*:
    UDP 1110  primary DS robot control
    UDP 1115  DS robot control (alternate)
    TCP 1740  DS dashboard data stream
"""

import argparse
import getpass
import json
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# ── FRC constants ─────────────────────────────────────────────────────────────

DS_CONTROL_PORTS: list[tuple[str, int]] = [
    ("udp", 1110),
    ("udp", 1115),
    ("tcp", 1740),
]
WIFI_IFACES        = ("wlan0", "wlan1")
BLOCK_SCRIPT_PATH  = "/etc/frc_ds_block.sh"
UCI_INCLUDE_NAME   = "frc_ds_block"
GLINET_DEFAULT_IP  = "192.168.8.1"
TESTED_MODEL_ID    = "GL-SFT1200"   # GL-iNet Opal
TESTED_MODEL_NAME  = "GL-iNet Opal (GL-SFT1200)"

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "-o", "PreferredAuthentications=password",
    "-o", "ConnectTimeout=8",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=3",
]

# ── Terminal colours ──────────────────────────────────────────────────────────

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RED    = "\033[31m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    CYAN   = "\033[36m"

def _ok(msg: str)   -> None: print(f"  {C.GREEN}✓{C.RESET}  {msg}")
def _warn(msg: str) -> None: print(f"  {C.YELLOW}⚠{C.RESET}  {msg}")
def _err(msg: str)  -> None: print(f"  {C.RED}✗{C.RESET}  {msg}")
def _step(msg: str) -> None: print(f"\n{C.BOLD}{C.CYAN}{msg}{C.RESET}")
def _dim(msg: str)  -> None: print(f"  {C.DIM}{msg}{C.RESET}")
def _hr(char: str = "─", width: int = 58) -> None: print(C.DIM + char * width + C.RESET)

# ── Network detection ─────────────────────────────────────────────────────────

def detect_default_gateway() -> str | None:
    """Return the default gateway IP, or None if it cannot be determined."""
    try:
        out = subprocess.check_output(
            ["ip", "route", "show", "default"], text=True, timeout=4,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            m = re.search(r"default via (\S+)", line)
            if m:
                return m.group(1)
    except Exception:
        pass

    # macOS
    try:
        out = subprocess.check_output(
            ["route", "-n", "get", "default"], text=True, timeout=4,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            m = re.match(r"\s*gateway:\s+(\S+)", line)
            if m:
                return m.group(1)
    except Exception:
        pass

    # Windows / PowerShell
    try:
        out = subprocess.check_output(
            ["powershell", "-Command",
             "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1).NextHop"],
            text=True, timeout=5, stderr=subprocess.DEVNULL,
        )
        gw = out.strip()
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", gw):
            return gw
    except Exception:
        pass

    return None


def probe_glinet(host: str, timeout: int = 4) -> bool:
    """
    Return True if the host looks like a GL-iNet router.
    Tries the JSON-RPC challenge first (works on configured routers), then falls
    back to checking the root HTTP page for GL-iNet HTML fingerprints (works on
    first-boot routers whose RPC endpoint is not yet active).
    """
    # Stage 1: JSON-RPC challenge — reliable on configured routers
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "challenge",
        "params": {"username": "root"},
    }).encode()
    try:
        req = urllib.request.Request(
            f"http://{host}/rpc",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
            result = body.get("result") or {}
            if "nonce" in result:
                return True
    except Exception:
        pass

    # Stage 2: HTML fingerprint — catches first-boot routers (no RPC yet)
    try:
        req = urllib.request.Request(
            f"http://{host}/",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="ignore").lower()
            return "gl-inet" in content or "gl.inet" in content
    except Exception:
        return False


def probe_no_password(host: str, timeout: int = 5) -> bool:
    """
    Return True if the router accepts SSH with an empty password.
    This indicates a brand-new GL-iNet that has never had its admin
    password set through the web UI first-boot wizard.
    """
    askpass = _make_askpass("")
    try:
        env = _ssh_env(askpass)
        r = subprocess.run(
            ["ssh"] + SSH_OPTS + [f"root@{host}", "echo ok"],
            capture_output=True, text=True, env=env, timeout=timeout + 2,
        )
        return r.returncode == 0 and "ok" in r.stdout
    except Exception:
        return False
    finally:
        _rm_askpass(askpass)


def probe_ssh_port(host: str, timeout: int = 3) -> bool:
    """Return True if TCP port 22 is open on host."""
    try:
        with socket.create_connection((host, 22), timeout=timeout):
            return True
    except Exception:
        return False

# ── SSH helpers ───────────────────────────────────────────────────────────────

def _make_askpass(password: str) -> str:
    """Write a temporary SSH_ASKPASS helper script; returns its path."""
    fd, path = tempfile.mkstemp(suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(password)}\n")
    os.chmod(path, stat.S_IRWXU)
    return path


def _rm_askpass(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _ssh_env(askpass_path: str) -> dict:
    env = os.environ.copy()
    env["SSH_ASKPASS"] = askpass_path
    env["SSH_ASKPASS_REQUIRE"] = "force"   # OpenSSH ≥ 8.4
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0"              # fallback for older OpenSSH
    return env


# ── Router class ──────────────────────────────────────────────────────────────

class GlinetRouter:
    """Configure a GL-iNet router via SSH using uci/iptables."""

    def __init__(self, host: str, password: str, debug: bool = False):
        self.host     = host
        self.password = password
        self.debug    = debug
        self._askpass: str | None = None

    # -- SSH transport ---------------------------------------------------------

    def _open(self) -> None:
        """Create the SSH_ASKPASS helper (call before any _run)."""
        if self._askpass is None:
            self._askpass = _make_askpass(self.password)

    def close(self) -> None:
        if self._askpass:
            _rm_askpass(self._askpass)
            self._askpass = None

    def _run(self, command: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a single command on the router over SSH."""
        self._open()
        env = _ssh_env(self._askpass)
        if self.debug:
            print(f"  {C.DIM}[ssh] {command}{C.RESET}")
        result = subprocess.run(
            ["ssh"] + SSH_OPTS + [f"root@{self.host}", command],
            capture_output=True, text=True, env=env, timeout=30,
        )
        if self.debug and (result.stdout or result.stderr):
            for line in (result.stdout + result.stderr).strip().splitlines():
                print(f"  {C.DIM}      {line}{C.RESET}")
        if check and result.returncode != 0:
            raise RuntimeError(
                f"SSH command failed (rc={result.returncode}): {command!r}\n"
                f"  stderr: {result.stderr.strip()}"
            )
        return result

    def _run_script(self, script: str) -> subprocess.CompletedProcess:
        """Pipe a multi-line shell script to the router via SSH stdin."""
        self._open()
        env = _ssh_env(self._askpass)
        if self.debug:
            for line in script.strip().splitlines():
                print(f"  {C.DIM}[sh] {line}{C.RESET}")
        result = subprocess.run(
            ["ssh"] + SSH_OPTS + [f"root@{self.host}", "sh"],
            input=script, capture_output=True, text=True, env=env, timeout=30,
        )
        if self.debug and (result.stdout or result.stderr):
            for line in (result.stdout + result.stderr).strip().splitlines():
                print(f"  {C.DIM}     {line}{C.RESET}")
        if result.returncode != 0:
            raise RuntimeError(
                f"Script failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        return result

    # -- Auth ------------------------------------------------------------------

    def login(self) -> None:
        """
        Verify SSH connectivity.  Raises a descriptive exception that
        distinguishes three failure categories:
          - Network error  : host unreachable, connection refused, timeout
          - Auth error     : wrong password / key rejected
          - Other SSH error: anything else
        """
        try:
            r = self._run("echo ssh_ok", check=False)
        except subprocess.TimeoutExpired:
            raise ConnectionError(
                f"SSH connection to {self.host} timed out.\n"
                "  Check that the router is powered on and reachable on the network."
            )

        if r.returncode == 0 and "ssh_ok" in r.stdout:
            return  # success

        stderr = r.stderr.strip().lower()

        # Network-level failures (SSH exits 255 for transport errors)
        network_clues = (
            "no route to host",
            "connection refused",
            "connection timed out",
            "network is unreachable",
            "could not resolve hostname",
            "name or service not known",
        )
        if r.returncode == 255 and any(c in stderr for c in network_clues):
            raise ConnectionError(
                f"Cannot reach {self.host} over the network.\n"
                f"  {r.stderr.strip()}\n"
                "  Check: Is the router powered on? Is your Ethernet cable plugged in?"
            )

        # Authentication failures
        auth_clues = ("permission denied", "authentication failed", "publickey,password")
        if any(c in stderr for c in auth_clues):
            raise PermissionError(
                f"SSH authentication rejected by {self.host}.\n"
                "  The password is incorrect, or SSH password auth is disabled on the router."
            )

        # Catch-all
        raise RuntimeError(
            f"SSH to {self.host} failed (exit {r.returncode}).\n"
            f"  {r.stderr.strip()}"
        )

    # -- Board info ------------------------------------------------------------

    def get_board_info(self) -> dict:
        try:
            r = self._run("cat /etc/board.json 2>/dev/null || true", check=False)
            if r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
        # Fall back to uci/system info
        try:
            r = self._run("uci get system.@system[0].hostname 2>/dev/null; "
                          "cat /tmp/sysinfo/board_name 2>/dev/null || true", check=False)
            return {"hostname": r.stdout.strip()}
        except Exception:
            return {}

    def get_model(self) -> str:
        """Return the router model ID string, e.g. 'GL-SFT1200'."""
        try:
            r = self._run(
                "jsonfilter -i /etc/board.json -e '@.model.id' 2>/dev/null "
                "|| cat /tmp/sysinfo/model 2>/dev/null || true",
                check=False,
            )
            return r.stdout.strip()
        except Exception:
            return ""

    # -- LAN -------------------------------------------------------------------

    def configure_lan(self, router_ip: str) -> None:
        print(f"  Configuring LAN → {router_ip}/255.255.255.0")
        self._run_script(f"""
uci set network.lan.ipaddr='{router_ip}'
uci set network.lan.netmask='255.255.255.0'
uci set network.lan.proto='static'
uci commit network
""")
        _ok("LAN IP configured.")

    # -- DHCP ------------------------------------------------------------------

    def configure_dhcp(self, team: int, router_ip: str) -> None:
        te, am = team // 100, team % 100
        dhcp_start, dhcp_limit = 20, 180
        print(
            f"  Configuring DHCP: 10.{te}.{am}.{dhcp_start}"
            f" – 10.{te}.{am}.{dhcp_start + dhcp_limit - 1}  (lease=12h)"
        )
        self._run_script(f"""
uci set dhcp.lan.interface='lan'
uci set dhcp.lan.start='{dhcp_start}'
uci set dhcp.lan.limit='{dhcp_limit}'
uci set dhcp.lan.leasetime='12h'
uci delete dhcp.lan.dhcp_option 2>/dev/null; true
uci add_list dhcp.lan.dhcp_option='3,{router_ip}'
uci add_list dhcp.lan.dhcp_option='6,{router_ip}'
uci commit dhcp
""")
        _ok("DHCP configured.")

    # -- WiFi AP ---------------------------------------------------------------

    def configure_wireless(self, ssid: str | None, wifi_password: str | None) -> None:
        if not ssid:
            return
        print(f"  Configuring WiFi AP → SSID={ssid}")
        enc   = "psk2" if wifi_password else "none"
        key_line = f"uci set wireless.default_radio0.key='{wifi_password}'" if wifi_password else ""
        self._run_script(f"""
uci set wireless.default_radio0.ssid='{ssid}'
uci set wireless.default_radio0.mode='ap'
uci set wireless.default_radio0.encryption='{enc}'
{key_line}
uci set wireless.default_radio0.disabled='0'
uci commit wireless
""")
        _ok("WiFi AP configured.")

    # -- DS WiFi block ---------------------------------------------------------

    def _block_script_body(self) -> str:
        lines = [
            "#!/bin/sh",
            "# FRC DS WiFi control block — managed by configure_frc_router.py",
        ]
        for iface in WIFI_IFACES:
            for proto, port in DS_CONTROL_PORTS:
                check  = f"iptables -C FORWARD -i {iface} -p {proto} --dport {port} -j DROP 2>/dev/null"
                insert = f"iptables -I FORWARD -i {iface} -p {proto} --dport {port} -j DROP"
                lines.append(f"{check} || {insert}")
        return "\n".join(lines) + "\n"

    def configure_ds_wifi_block(self, block: bool = True) -> None:
        ports_desc = ", ".join(f"{p.upper()} {n}" for p, n in DS_CONTROL_PORTS)
        action = "Enabling" if block else "Disabling"
        print(f"  {action} DS WiFi block ({ports_desc})")

        if block:
            body = self._block_script_body()
            # Write block script then wire up the UCI firewall include
            setup = f"""cat > {BLOCK_SCRIPT_PATH} << 'FRCEOF'
{body}
FRCEOF
chmod +x {BLOCK_SCRIPT_PATH}
{BLOCK_SCRIPT_PATH}
uci set firewall.{UCI_INCLUDE_NAME}='include'
uci set firewall.{UCI_INCLUDE_NAME}.path='{BLOCK_SCRIPT_PATH}'
uci set firewall.{UCI_INCLUDE_NAME}.type='script'
uci set firewall.{UCI_INCLUDE_NAME}.reload='1'
uci commit firewall
"""
            self._run_script(setup)
            _ok("DS WiFi block enabled — rules active and persistent.")
        else:
            self._run_script(f"""
for iface in {" ".join(WIFI_IFACES)}; do
  {"".join(f"iptables -D FORWARD -i $iface -p {p} --dport {n} -j DROP 2>/dev/null; " for p,n in DS_CONTROL_PORTS)}
done
uci delete firewall.{UCI_INCLUDE_NAME} 2>/dev/null; true
uci commit firewall
rm -f {BLOCK_SCRIPT_PATH}
""")
            _ok("DS WiFi block removed.")

    # -- Restart ---------------------------------------------------------------

    def restart_services(self) -> None:
        """
        Trigger a restart of network/dnsmasq/firewall on the router.  Because
        the LAN IP usually changes here, the SSH connection will be dropped
        mid-command — that's expected, not a failure.  We detach the restart
        from this shell so it survives the disconnect, and we swallow the
        inevitable broken-pipe error.
        """
        print("  Restarting network and firewall services…")
        # The `nohup ... </dev/null >/dev/null 2>&1 &` pattern detaches the
        # subshell from SSH; `sleep 1` lets our SSH command return cleanly
        # before the network goes down.  We do not wait for completion.
        script = (
            "nohup sh -c 'sleep 1; "
            "/etc/init.d/network restart; "
            "/etc/init.d/dnsmasq restart; "
            "/etc/init.d/firewall restart' "
            "</dev/null >/dev/null 2>&1 &\n"
        )
        try:
            self._run_script(script)
        except RuntimeError as e:
            # Broken pipe / connection-reset is the expected outcome here.
            msg = str(e).lower()
            if "broken pipe" in msg or "connection" in msg or "rc=255" in msg:
                pass
            else:
                raise
        _ok("Restart triggered — SSH will disconnect as the network reloads.")

    # -- Summary ---------------------------------------------------------------

    def print_summary(self, team: int, router_ip: str, block_ds_wifi: bool,
                      ssid: str | None, model: str) -> None:
        te, am = team // 100, team % 100
        ds_line   = "ENABLED  (DS must use Ethernet)" if block_ds_wifi else "disabled"
        ssid_line = ssid if ssid else "(not configured)"
        W = 60
        def row(label: str, value: str) -> str:
            content = f"  {label:<18}: {value}"
            return f"║{content:<{W}}║"

        print(f"\n╔{'═' * W}╗")
        print(f"║{'  FRC Team ' + str(team) + ' — Configuration Applied':<{W}}║")
        print(f"╠{'═' * W}╣")
        print(row("Router model",   model or "Unknown"))
        print(row("Router IP",      router_ip))
        print(row("Subnet",         "255.255.255.0"))
        print(f"╠{'═' * W}╣")
        print(row(f"10.{te}.{am}.1", "VH-109 Radio  (field kiosk)"))
        print(row(f"10.{te}.{am}.2", "roboRIO"))
        print(row(f"10.{te}.{am}.3", "Field network (reserved)"))
        print(row(f"10.{te}.{am}.4", "This router"))
        print(row(f"10.{te}.{am}.5", "Driver Station"))
        print(row(f"10.{te}.{am}.6–19", "Buffer — other static devices"))
        print(f"╠{'═' * W}╣")
        print(row("DHCP pool",      f"10.{te}.{am}.20 – 10.{te}.{am}.199  (12 h lease)"))
        print(row("WiFi AP SSID",   ssid_line))
        print(row("DS WiFi block",  ds_line))
        print(f"╚{'═' * W}╝")

# ── First-boot helper ────────────────────────────────────────────────────────

def _warn_no_password(host: str) -> None:
    """
    Print first-boot setup instructions and exit.
    Called when we detect a GL-iNet router that has no admin password set.
    """
    print()
    print(f"  {C.BOLD}{C.YELLOW}┌─ ROUTER NOT SET UP ────────────────────────────────────┐{C.RESET}")
    print(f"  {C.BOLD}{C.YELLOW}│{C.RESET}  This GL-iNet router has no admin password set.         {C.BOLD}{C.YELLOW}│{C.RESET}")
    print(f"  {C.BOLD}{C.YELLOW}│{C.RESET}  You must complete the first-boot wizard in the web UI  {C.BOLD}{C.YELLOW}│{C.RESET}")
    print(f"  {C.BOLD}{C.YELLOW}│{C.RESET}  before this configurator can connect via SSH.           {C.BOLD}{C.YELLOW}│{C.RESET}")
    print(f"  {C.BOLD}{C.YELLOW}└────────────────────────────────────────────────────────┘{C.RESET}")
    print()
    print(f"  {C.BOLD}How to complete first-time router setup:{C.RESET}")
    print()
    print(f"   1. Make sure your computer is connected to the router via Ethernet")
    print(f"      or WiFi (look for a network named GL-SFT1200-xxxx).")
    print(f"   2. Open a web browser and go to:  {C.BOLD}http://{host}{C.RESET}")
    print( "   3. Choose your language and click Next.")
    print( "   4. Set a strong admin password and click Apply.")
    print( "   5. Re-run this configurator — it will log in with the password you set.")
    print()
    sys.exit(0)

# ── Interactive prompts ───────────────────────────────────────────────────────

def _prompt(label: str, default: str = "", required: bool = True) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        val = input(f"  {C.BOLD}{label}{hint}:{C.RESET} ").strip()
        if not val and default:
            return default
        if val or not required:
            return val
        _err("This field is required.")


def _prompt_int(label: str, lo: int, hi: int, default: int | None = None) -> int:
    hint = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"  {C.BOLD}{label}{hint}:{C.RESET} ").strip()
        if not raw and default is not None:
            return default
        try:
            val = int(raw)
            if lo <= val <= hi:
                return val
            _err(f"Must be between {lo} and {hi}.")
        except ValueError:
            _err("Please enter a number.")


def _prompt_bool(label: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        raw = input(f"  {C.BOLD}{label}{C.RESET} {C.DIM}{hint}{C.RESET} ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        _err("Please enter y or n.")


def _prompt_password(label: str = "Router SSH/admin password") -> str:
    while True:
        try:
            pw = getpass.getpass(f"  {C.BOLD}{label}:{C.RESET} ")
        except Exception:
            pw = input(f"  {C.BOLD}{label}:{C.RESET} ").strip()
        if pw:
            return pw
        _err("Password cannot be empty.")

# ── Interactive configurator ──────────────────────────────────────────────────

def run_interactive(cli: argparse.Namespace) -> dict:
    print(f"\n{C.BOLD}{C.CYAN}╔══════════════════════════════════════════════════╗{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}║      FRC GL-iNet Router Configurator             ║{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}╚══════════════════════════════════════════════════╝{C.RESET}")

    # ── 1. Detect / confirm router host ──────────────────────────────────────
    _step("Step 1 of 5 — Locate the router")

    router_host: str | None = cli.router_host

    if not router_host:
        detected_gw = detect_default_gateway()

        if detected_gw:
            print(f"  Detected default gateway: {C.BOLD}{detected_gw}{C.RESET}")
            print(f"  Probing {detected_gw} for GL-iNet firmware…")
            if probe_glinet(detected_gw):
                _ok(f"GL-iNet router confirmed at {detected_gw}")
                router_host = detected_gw
                if not probe_ssh_port(router_host) or probe_no_password(router_host):
                    _warn_no_password(router_host)
            else:
                _warn(f"{detected_gw} did not respond as a GL-iNet router.")
                print(f"  Trying GL-iNet factory default ({GLINET_DEFAULT_IP})…")
                if probe_glinet(GLINET_DEFAULT_IP):
                    _ok(f"GL-iNet router found at factory default {GLINET_DEFAULT_IP}")
                    router_host = GLINET_DEFAULT_IP
                    if not probe_ssh_port(router_host) or probe_no_password(router_host):
                        _warn_no_password(router_host)
                else:
                    _warn(f"No GL-iNet router found at {detected_gw} or {GLINET_DEFAULT_IP}.")
        else:
            _warn("Could not determine default gateway.")
            print(f"  Trying GL-iNet factory default ({GLINET_DEFAULT_IP})…")
            if probe_glinet(GLINET_DEFAULT_IP):
                _ok(f"GL-iNet router found at {GLINET_DEFAULT_IP}")
                router_host = GLINET_DEFAULT_IP
                if not probe_ssh_port(router_host) or probe_no_password(router_host):
                    _warn_no_password(router_host)
            else:
                _warn(f"No GL-iNet router found at {GLINET_DEFAULT_IP}.")

        if not router_host:
            _warn("Could not auto-detect the router.")
            router_host = _prompt("Enter router IP address", default=GLINET_DEFAULT_IP)
    else:
        # --router-host was given explicitly; still probe to confirm it's GL-iNet
        print(f"  Probing {router_host} for GL-iNet firmware…")
        if probe_glinet(router_host):
            _ok(f"GL-iNet router confirmed at {router_host}")
            if not probe_ssh_port(router_host) or probe_no_password(router_host):
                _warn_no_password(router_host)
        else:
            _warn(f"{router_host} did not respond as a GL-iNet router.")
            _warn("Continuing anyway — SSH commands may still work on non-GL-iNet OpenWrt.")

    _ok(f"Target router: {router_host}")

    # ── 2. Login & model check ────────────────────────────────────────────────
    _step("Step 2 of 5 — Authenticate via SSH")

    password = cli.password or _prompt_password()
    router = GlinetRouter(host=router_host, password=password, debug=cli.debug)

    for attempt in range(1, 4):
        print(f"  Connecting to root@{router_host} via SSH…")
        try:
            router.login()
            break
        except ConnectionError as e:
            # Network problem — no point retrying with a different password
            _err(str(e))
            router.close()
            sys.exit(1)
        except PermissionError as e:
            _err(str(e))
            if attempt == 3:
                _err("Too many failed attempts.")
                router.close()
                sys.exit(1)
            router.password = _prompt_password("Try again — router SSH password")
        except Exception as e:
            _err(f"Unexpected SSH error (attempt {attempt}/3): {e}")
            if cli.debug:
                import traceback
                _dim(traceback.format_exc())
            if attempt == 3:
                router.close()
                sys.exit(1)
            router.password = _prompt_password("Try again — router SSH password")
    _ok("SSH connection established.")

    print("  Reading router model…")
    model = router.get_model()
    if model:
        _ok(f"Model: {model}")
    else:
        _warn("Could not read model.")

    if model and TESTED_MODEL_ID.lower() not in model.lower():
        print()
        _warn(f"This router ({model}) has not been tested with this script.")
        _warn(f"Only the {TESTED_MODEL_NAME} is officially supported.")
        if not _prompt_bool("Continue anyway?", default=False):
            router.close()
            print("  Aborted.")
            sys.exit(0)

    # ── 3. Team number ────────────────────────────────────────────────────────
    _step("Step 3 of 5 — FRC team settings")

    team = cli.team or _prompt_int("Team number", lo=1, hi=9999)
    router_ip = build_router_ip(team)
    te, am = team // 100, team % 100
    _ok(f"Router will be configured as {router_ip}")
    _dim(f"Static addresses: 10.{te}.{am}.1 (radio) · .2 (roboRIO) · .4 (router) · .5 (DS)")
    _dim(f"DHCP pool       : 10.{te}.{am}.20 – 10.{te}.{am}.199")

    # ── 4. WiFi AP ────────────────────────────────────────────────────────────
    _step("Step 4 of 5 — WiFi AP  (optional — for laptop tethering)")
    _dim("Configure a WiFi AP if you want devices to connect wirelessly.")
    _dim("The DS should still use Ethernet for robot control.")

    wifi_ssid: str | None = cli.wifi_ssid
    wifi_password: str | None = cli.wifi_password

    if not wifi_ssid:
        if _prompt_bool("Configure WiFi AP?", default=False):
            wifi_ssid = _prompt("WiFi SSID", required=True)
            wifi_password = _prompt("WiFi password (leave blank for open network)",
                                    required=False) or None
            if wifi_password:
                _ok("WPA2 password set.")
            else:
                _warn("Open network — no WiFi password.")
        else:
            _ok("WiFi AP skipped.")

    # ── 5. DS WiFi block ──────────────────────────────────────────────────────
    _step("Step 5 of 5 — Driver Station WiFi block")
    _dim("Blocks UDP 1110, UDP 1115, TCP 1740 from wlan0/wlan1 → prevents a WiFi-")
    _dim("connected laptop from accidentally driving the robot.")

    if cli.allow_ds_wifi:
        block_ds = False
        _warn("DS WiFi block disabled via --allow-ds-wifi.")
    else:
        block_ds = _prompt_bool("Block DS control traffic on WiFi?", default=True)

    if block_ds:
        _ok("DS WiFi block will be enabled.")
    else:
        _warn("DS WiFi block will NOT be enabled.")

    # ── Confirm ───────────────────────────────────────────────────────────────
    _step("Review")
    _hr()
    print(f"  Router host   : {router_host}")
    print(f"  Model         : {model or 'unknown'}")
    print(f"  Team          : {team}  →  router IP {router_ip}")
    print(f"  DHCP pool     : 10.{te}.{am}.20 – 10.{te}.{am}.199")
    print(f"  WiFi SSID     : {wifi_ssid or '(none)'}")
    print(f"  DS WiFi block : {'yes' if block_ds else 'no'}")
    _hr()
    print()

    if not _prompt_bool("Apply this configuration?", default=True):
        router.close()
        print("  Aborted — no changes made.")
        sys.exit(0)

    return {
        "router":        router,
        "router_ip":     router_ip,
        "team":          team,
        "wifi_ssid":     wifi_ssid,
        "wifi_password": wifi_password,
        "block_ds_wifi": block_ds,
        "model":         model,
    }

# ── Apply config ──────────────────────────────────────────────────────────────

def build_router_ip(team: int) -> str:
    return f"10.{team // 100}.{team % 100}.4"


def apply_config(cfg: dict) -> None:
    router: GlinetRouter = cfg["router"]
    current_ip = router.host
    new_ip     = cfg["router_ip"]
    ip_changes = current_ip != new_ip

    _step("Applying configuration…")
    router.configure_lan(router_ip=new_ip)
    router.configure_dhcp(team=cfg["team"], router_ip=new_ip)
    router.configure_wireless(ssid=cfg["wifi_ssid"], wifi_password=cfg["wifi_password"])
    router.configure_ds_wifi_block(block=cfg["block_ds_wifi"])
    router.restart_services()
    router.close()

    router.print_summary(
        team=cfg["team"],
        router_ip=new_ip,
        block_ds_wifi=cfg["block_ds_wifi"],
        ssid=cfg["wifi_ssid"],
        model=cfg["model"],
    )

    print()
    if ip_changes:
        print(f"  {C.YELLOW}{C.BOLD}IP changed: {current_ip} → {new_ip}{C.RESET}")
    else:
        print(f"  Router IP: {C.BOLD}{new_ip}{C.RESET}")
    print()
    print(f"  {C.BOLD}Next step — reconnect your computer to the router:{C.RESET}")
    print( "   1. The router has restarted on its new IP and is now serving DHCP")
    print(f"      in the {C.BOLD}{new_ip.rsplit('.', 1)[0]}.20–.199{C.RESET} range.")
    print( "   2. If you're on Ethernet: release/renew your DHCP lease, or unplug")
    print( "      and replug the cable to pick up a new address.")
    print( "   3. If you were on WiFi: re-associate with the router's WiFi.")
    print()

    # Offer to verify reachability — keep retrying until the user confirms or quits.
    _verify_reachable(new_ip)

    print(f"\n{C.GREEN}{C.BOLD}Done!{C.RESET}  Router is reachable at "
          f"{C.BOLD}http://{new_ip}{C.RESET}")


def _verify_reachable(host: str) -> None:
    """
    Probe the router at the new IP and let the user retry while they reconnect.
    Skips silently if the user declines to verify.
    """
    if not _prompt_bool(
        f"Verify the router is reachable at {host}?", default=True,
    ):
        return

    while True:
        print(f"  Probing {host} …")
        port_open  = probe_ssh_port(host, timeout=4)
        is_glinet  = probe_glinet(host, timeout=4) if port_open else False

        if port_open and is_glinet:
            _ok(f"Router reachable at {host} (SSH + HTTP responding).")
            return
        if port_open:
            _ok(f"Router reachable at {host} (SSH responding).")
            return

        _warn(f"No response from {host} yet.")
        print( "  This is normal if your computer hasn't picked up a new DHCP")
        print( "  lease yet.  Try renewing your IP, then retry.")
        if not _prompt_bool("Retry?", default=True):
            _warn("Skipping verification — configuration was applied but not verified.")
            return

# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Configure a GL-iNet router for FRC robot networking via SSH. "
            "Run without arguments for the interactive configurator."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--team", type=int, default=None,
                        help="FRC team number (e.g. 620 → router IP 10.6.20.4)")
    parser.add_argument("--router-host", default=None,
                        help=f"Router IP/hostname (default: auto-detect gateway)")
    parser.add_argument("--password", default=None,
                        help="Router root/SSH password")
    parser.add_argument("--wifi-ssid", default=None,
                        help="Configure the WiFi radio as an AP with this SSID")
    parser.add_argument("--wifi-password", default=None,
                        help="WPA2 passphrase for the WiFi AP (omit for open network)")
    parser.add_argument("--allow-ds-wifi", action="store_true", default=False,
                        help="Allow DS traffic over WiFi (default: block it)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be configured without touching the router")
    parser.add_argument("--debug", action="store_true",
                        help="Print SSH commands and output for diagnostics")
    args = parser.parse_args()

    # Fully non-interactive path
    cli_complete = args.team is not None and args.password is not None
    if cli_complete and not args.dry_run:
        if not 1 <= args.team <= 9999:
            parser.error("Team number must be 1–9999.")
        router_ip   = build_router_ip(args.team)
        router_host = args.router_host or detect_default_gateway() or GLINET_DEFAULT_IP
        router = GlinetRouter(host=router_host, password=args.password, debug=args.debug)
        try:
            router.login()
        except (ConnectionError, PermissionError, RuntimeError) as e:
            _err(str(e))
            router.close()
            sys.exit(1)
        model = router.get_model()
        try:
            apply_config({
                "router":        router,
                "router_ip":     router_ip,
                "team":          args.team,
                "wifi_ssid":     args.wifi_ssid,
                "wifi_password": args.wifi_password,
                "block_ds_wifi": not args.allow_ds_wifi,
                "model":         model,
            })
        except (ConnectionError, RuntimeError) as e:
            _err(str(e))
            router.close()
            sys.exit(1)
        return

    if args.dry_run:
        if args.team is None:
            parser.error("--dry-run requires --team.")
        router_ip = build_router_ip(args.team)
        print("[DRY RUN] No changes will be made.\n")
        r = GlinetRouter.__new__(GlinetRouter)
        r.print_summary(
            team=args.team,
            router_ip=router_ip,
            block_ds_wifi=not args.allow_ds_wifi,
            ssid=args.wifi_ssid,
            model="(dry run)",
        )
        return

    cfg = run_interactive(args)
    apply_config(cfg)


if __name__ == "__main__":
    main()
