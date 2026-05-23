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
import base64
import json
import sys
import time
import urllib.request
import urllib.error

# FRC DS→Robot control ports to block from WiFi interfaces
DS_CONTROL_PORTS: list[tuple[str, int]] = [
    ("udp", 1110),
    ("udp", 1115),
    ("tcp", 1740),
]

# GL-iNet exposes both the 2.4 GHz and 5 GHz radios as separate interfaces
WIFI_IFACES = ("wlan0", "wlan1")

BLOCK_SCRIPT_PATH = "/etc/frc_ds_block.sh"
UCI_INCLUDE_NAME  = "frc_ds_block"


def build_router_ip(team: int) -> str:
    return f"10.{team // 100}.{team % 100}.4"


class GlinetRouter:
    """Thin wrapper around the GL-iNet HTTP JSON-RPC API (gl-sdk4 / ubus proxy)."""

    def __init__(self, host: str, password: str, timeout: int = 15):
        self.base = f"http://{host}"
        self.password = password
        self.timeout = timeout
        self.sid: str | None = None

    # ------------------------------------------------------------------
    # Low-level RPC
    # ------------------------------------------------------------------

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        }).encode()

        headers = {"Content-Type": "application/json"}
        if self.sid:
            headers["Authorization"] = f"Bearer {self.sid}"

        req = urllib.request.Request(f"{self.base}/rpc", data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.URLError as e:
            print(f"[ERROR] Could not reach router at {self.base}: {e}", file=sys.stderr)
            sys.exit(1)

        if "error" in body:
            raise RuntimeError(f"RPC error: {body['error']}")
        return body.get("result", {})

    def _ubus(self, path: str, method: str, params: dict | None = None) -> dict:
        return self._rpc("call", {"path": path, "method": method, "params": params or {}})

    def _exec(self, cmd: str) -> None:
        """Run a shell command on the router via ubus system/exec."""
        self._ubus("system", "exec", {"command": f"/bin/sh -c {json.dumps(cmd)}"})

    def _exec_script(self, script: str) -> None:
        """Base64-encode a multi-line script and pipe it into sh to avoid quoting issues."""
        b64 = base64.b64encode(script.encode()).decode()
        self._exec(f"echo '{b64}' | base64 -d | sh")

    # ------------------------------------------------------------------
    # UCI helpers
    # ------------------------------------------------------------------

    def _uci_set(self, config: str, section: str, values: dict) -> None:
        self._ubus("uci", "set", {"config": config, "section": section, "values": values})

    def _uci_delete(self, config: str, section: str) -> None:
        self._ubus("uci", "delete", {"config": config, "section": section})

    def _uci_commit(self, config: str) -> None:
        self._ubus("uci", "commit", {"config": config})

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self) -> None:
        result = self._rpc("login", {"username": "root", "password": self.password})
        self.sid = result.get("sid") or result.get("token")
        if not self.sid:
            print("[ERROR] Login failed — check your password.", file=sys.stderr)
            sys.exit(1)
        print(f"[OK] Logged in (session={self.sid[:8]}…)")

    # ------------------------------------------------------------------
    # LAN
    # ------------------------------------------------------------------

    def configure_lan(self, router_ip: str) -> None:
        print(f"\n[*] Configuring LAN interface → {router_ip}/255.255.255.0")
        self._uci_set("network", "lan", {
            "ipaddr": router_ip,
            "netmask": "255.255.255.0",
            "proto": "static",
        })
        self._uci_commit("network")
        print("[OK] LAN IP configured.")

    # ------------------------------------------------------------------
    # DHCP
    # ------------------------------------------------------------------

    def configure_dhcp(self, team: int, router_ip: str) -> None:
        """
        Pool starts at .20 to leave room for all reserved/static addresses:
          .1  radio   .2  roboRIO   .3  field   .4  router   .5  DS   .6–.19  buffer
        Pool ends at .199 (180 leases).
        """
        te, am = team // 100, team % 100
        dhcp_start, dhcp_limit = 20, 180

        print(
            f"\n[*] Configuring DHCP: 10.{te}.{am}.{dhcp_start}"
            f" – 10.{te}.{am}.{dhcp_start + dhcp_limit - 1}  (lease=12h)"
        )
        self._uci_set("dhcp", "lan", {
            "interface": "lan",
            "start": str(dhcp_start),
            "limit": str(dhcp_limit),
            "leasetime": "12h",
            "dhcpv6": "disabled",
            "dhcp_option": [
                f"3,{router_ip}",   # default gateway
                f"6,{router_ip}",   # DNS server
            ],
        })
        self._uci_commit("dhcp")
        print("[OK] DHCP configured.")

    # ------------------------------------------------------------------
    # WiFi AP
    # ------------------------------------------------------------------

    def configure_wireless(self, ssid: str | None, wifi_password: str | None) -> None:
        if not ssid:
            return
        print(f"\n[*] Configuring WiFi AP → SSID={ssid}")
        self._uci_set("wireless", "default_radio0", {
            "ssid": ssid,
            "mode": "ap",
            "encryption": "psk2" if wifi_password else "none",
            **({"key": wifi_password} if wifi_password else {}),
        })
        self._uci_commit("wireless")
        print("[OK] WiFi AP configured.")

    # ------------------------------------------------------------------
    # DS WiFi block
    # ------------------------------------------------------------------

    def _block_script(self) -> str:
        """
        iptables rules that drop DS control packets arriving on any WiFi interface.
        Uses -C (check) before -I (insert) so rules aren't duplicated on firewall
        reload or router reboot.
        """
        lines = [
            "#!/bin/sh",
            "# FRC DS WiFi control block — managed by configure_frc_router.py",
        ]
        for iface in WIFI_IFACES:
            for proto, port in DS_CONTROL_PORTS:
                check = f"iptables -C FORWARD -i {iface} -p {proto} --dport {port} -j DROP 2>/dev/null"
                insert = f"iptables -I FORWARD -i {iface} -p {proto} --dport {port} -j DROP"
                lines.append(f"{check} || {insert}")
        return "\n".join(lines) + "\n"

    def _unblock_script(self) -> str:
        lines = ["#!/bin/sh", "# FRC DS WiFi unblock"]
        for iface in WIFI_IFACES:
            for proto, port in DS_CONTROL_PORTS:
                lines.append(
                    f"iptables -D FORWARD -i {iface} -p {proto} --dport {port} -j DROP 2>/dev/null; true"
                )
        return "\n".join(lines) + "\n"

    def configure_ds_wifi_block(self, block: bool = True) -> None:
        """
        Enable or disable blocking of FRC Driver Station control traffic from WiFi.

        When enabled:
          - Writes /etc/frc_ds_block.sh with the iptables rules
          - Applies the rules immediately
          - Adds a UCI firewall include so rules survive reboots and firewall restarts

        When disabled:
          - Removes the iptables rules immediately
          - Removes the UCI firewall include and the script file
        """
        action = "Enabling" if block else "Disabling"
        ports_desc = ", ".join(f"{proto.upper()} {port}" for proto, port in DS_CONTROL_PORTS)
        print(f"\n[*] {action} DS WiFi block ({ports_desc}) on {', '.join(WIFI_IFACES)}…")

        if block:
            setup = "\n".join([
                "#!/bin/sh",
                # Write and apply the persistent block script
                f"echo '{base64.b64encode(self._block_script().encode()).decode()}'"
                f" | base64 -d > {BLOCK_SCRIPT_PATH}",
                f"chmod +x {BLOCK_SCRIPT_PATH}",
                f"{BLOCK_SCRIPT_PATH}",
                # Register it as a UCI firewall include (named section via uci CLI)
                f"uci set firewall.{UCI_INCLUDE_NAME}=include",
                f"uci set firewall.{UCI_INCLUDE_NAME}.path={BLOCK_SCRIPT_PATH}",
                f"uci set firewall.{UCI_INCLUDE_NAME}.type=script",
                f"uci set firewall.{UCI_INCLUDE_NAME}.reload=1",
                "uci commit firewall",
            ])
            self._exec_script(setup)
            print("[OK] DS WiFi block enabled — rules active and persistent.")
        else:
            teardown = "\n".join([
                "#!/bin/sh",
                # Run the unblock script to flush the rules now
                f"echo '{base64.b64encode(self._unblock_script().encode()).decode()}'"
                f" | base64 -d | sh",
                # Remove the UCI include and the script file
                f"uci delete firewall.{UCI_INCLUDE_NAME} 2>/dev/null; true",
                "uci commit firewall",
                f"rm -f {BLOCK_SCRIPT_PATH}",
            ])
            self._exec_script(teardown)
            print("[OK] DS WiFi block removed.")

    # ------------------------------------------------------------------
    # Service restart
    # ------------------------------------------------------------------

    def restart_services(self) -> None:
        print("\n[*] Restarting network and firewall services (allow ~20 s)…")
        for svc in ("network", "dnsmasq", "firewall"):
            try:
                self._exec(f"/etc/init.d/{svc} restart")
            except Exception:
                pass  # connection may drop as network restarts
        time.sleep(8)
        print("[OK] Services restarted.")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def print_summary(self, team: int, router_ip: str, block_ds_wifi: bool) -> None:
        te, am = team // 100, team % 100
        ds_status = "ENABLED (DS must use Ethernet)" if block_ds_wifi else "disabled"
        ports_str = " | ".join(f"{p.upper()} {n}" for p, n in DS_CONTROL_PORTS)
        print(f"""
╔══════════════════════════════════════════════════════════════╗
║        FRC Team {team:<4} — Router Configuration Summary          ║
╠══════════════════════════════════════════════════════════════╣
║  Router (gateway)   : {router_ip:<38} ║
║  Subnet             : 255.255.255.0                          ║
╠══════════════════════════════════════════════════════════════╣
║  Static assignments (configure on each device manually):     ║
║    10.{te}.{am}.1    VH-109 Radio  (set by field kiosk)     ║
║    10.{te}.{am}.2    roboRIO                                 ║
║    10.{te}.{am}.3    Field network (reserved)                ║
║    10.{te}.{am}.4    This router                             ║
║    10.{te}.{am}.5    Driver Station                          ║
║    10.{te}.{am}.6–.19  Buffer for other static devices       ║
╠══════════════════════════════════════════════════════════════╣
║  DHCP pool  : 10.{te}.{am}.20 – 10.{te}.{am}.199  (180 leases)    ║
║  Lease time : 12 hours                                       ║
╠══════════════════════════════════════════════════════════════╣
║  DS WiFi block : {ds_status:<43} ║
║  Blocked ports : {ports_str:<43} ║
║  Interfaces    : {", ".join(WIFI_IFACES):<43} ║
╚══════════════════════════════════════════════════════════════╝
""")


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure a GL-iNet router for FRC robot networking.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--team", type=int, required=True,
        help="FRC team number (e.g. 620 → router IP 10.6.20.4)",
    )
    parser.add_argument(
        "--router-host",
        help="Current router address (default: 192.168.8.1, GL-iNet factory default)",
    )
    parser.add_argument(
        "--password", required=True,
        help="Router admin password (root user)",
    )
    parser.add_argument(
        "--wifi-ssid",
        help="Configure the 2.4 GHz radio as an AP with this SSID",
    )
    parser.add_argument(
        "--wifi-password",
        help="WPA2 passphrase for the WiFi AP (omit for open network)",
    )
    parser.add_argument(
        "--allow-ds-wifi", action="store_true", default=False,
        help=(
            "Allow Driver Station traffic over WiFi. "
            "By default DS control ports are blocked on wlan* to prevent "
            "accidental wireless robot control — use this flag to remove that restriction."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be configured without touching the router",
    )
    args = parser.parse_args()

    if not 1 <= args.team <= 9999:
        print("[ERROR] Team number must be 1–9999.", file=sys.stderr)
        sys.exit(1)

    block_ds_wifi = not args.allow_ds_wifi
    router_ip = build_router_ip(args.team)
    router_host = args.router_host or "192.168.8.1"

    print("FRC GL-iNet Router Configurator")
    print(f"  Team         : {args.team}")
    print(f"  Target IP    : {router_ip}")
    print(f"  Connecting   : http://{router_host}")
    print(f"  DS WiFi block: {'yes (default)' if block_ds_wifi else 'no (--allow-ds-wifi)'}")

    if args.dry_run:
        print("\n[DRY RUN] No changes will be made.")
        r = GlinetRouter.__new__(GlinetRouter)
        r.print_summary(args.team, router_ip, block_ds_wifi)
        return

    router = GlinetRouter(host=router_host, password=args.password)
    router.login()
    router.configure_lan(router_ip=router_ip)
    router.configure_dhcp(team=args.team, router_ip=router_ip)
    router.configure_wireless(ssid=args.wifi_ssid, wifi_password=args.wifi_password)
    router.configure_ds_wifi_block(block=block_ds_wifi)
    router.restart_services()
    router.print_summary(team=args.team, router_ip=router_ip, block_ds_wifi=block_ds_wifi)

    print(f"[DONE] Router is now reachable at http://{router_ip}")
    print("       Reconnect via Ethernet to verify.")


if __name__ == "__main__":
    main()
