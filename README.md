# FRC Robot Router Configurator

A configuration utility for a GL-iNet Opal (GL-SFT1200) travel router
intended for temporary installation on an FRC robot during development and
practice. The router provides internet access to devices on the robot's
network while leaving robot control traffic unaffected, and is removed
before official matches.

## Purpose

The robot's network (roboRIO, radio, Driver Station) is normally isolated
from the internet, and has no access to it during a match. During practice
and development, however, this isolation requires a laptop connected to the
robot network to disconnect and join a separate network in order to retrieve
or publish source code, consult vendor documentation, or perform similar
tasks, then reconnect to resume testing.

This router eliminates that requirement. It occupies the `10.TE.AM.4`
address, the position reserved in the FRC networking specification for the
field's own network equipment, and is configured as the default gateway for
the robot's network. Traffic from a device on the robot network that is not
addressed to another device on that network is routed through this router to
a separate internet uplink. Devices remain on the robot network for the
duration of this access.

Robot control traffic is not affected by this configuration:

- The roboRIO, radio, and Driver Station retain their standard static
  addresses and communicate as they would without the router present.
- The router's internet uplink is an outbound connection to a separate
  network. No port forwarding, DMZ, or similar mechanism is configured, and
  no path exists for a device on the internet to reach the robot network.
- A firewall rule is applied to the router's wireless interfaces to drop
  Driver Station control traffic (UDP 1110, UDP 1115, TCP 1740). A device
  connected only over the router's wireless interface can reach the internet
  but cannot issue robot control commands; control requires the standard
  wired Driver Station connection.

## Hardware

- Router: GL-iNet Opal, model ID `GL-SFT1200`. This is the only model against
  which the script has been tested. The unit is small enough to mount on the
  robot chassis and is powered from the robot's electrical system (for
  example, a 12V-to-5V/USB source) rather than a wall outlet once deployed.
- The router's wireless radio serves two distinct roles:
  - Uplink: the router joins an existing wireless network (venue network,
    mobile hotspot, or similar) to obtain internet access. This is
    configured through the GL-iNet web interface and is not performed by
    this script.
  - Local access point (optional): the script can additionally configure the
    router's radio as an access point, allowing devices to join the robot
    network wirelessly through the router if no other wireless access point
    is present on the robot.
- The router's LAN port connects to the robot's existing network (switch or
  radio), alongside the roboRIO. It is added as an additional device on that
  network rather than replacing any existing equipment.

## Requirements

- Python 3.10 or later (the script uses `X | None` type hints)
- An `ssh` client available on the system path
- A GL-iNet router that has completed its first-boot web setup (admin
  password configured). If it has not, the script detects this condition and
  prints setup instructions instead of proceeding.

## Deployment Procedure

The router is intended to be installed for development and practice sessions
only, and removed before official matches. It should be configured before
each installation on the robot.

1. Connect a laptop directly to the router, either over its wireless access
   point (`GL-SFT1200-xxxx`) or via Ethernet, while it is not yet installed
   on the robot.
2. Run the configurator:
   ```bash
   python3 configure_frc_router.py
   ```
   The script proceeds through five steps: locating the router, authenticating
   over SSH, requesting the team number (from which the `10.TE.AM.4` address
   and DHCP range are derived), optionally enabling the local wireless access
   point, and configuring the Driver Station wireless control block. A
   summary is presented for confirmation before any changes are applied.
3. Configure the router's internet uplink through the GL-iNet web interface
   (wireless client or repeater mode, joining venue Wi-Fi, a mobile hotspot,
   or similar). This step is not performed by the script, which manages only
   the robot-facing side of the configuration.
4. Disconnect the laptop and install the router on the robot: connect its LAN
   port to the robot's existing network and provide power from the robot's
   electrical system.
5. Devices that subsequently connect to the robot's network, whether over an
   existing wireless access point or a wired connection, obtain internet
   access through the router automatically. Robot control traffic is
   unaffected.
6. Before an official match, disconnect the router's LAN port and remove it
   from the robot. The robot's network reverts to its normal configuration,
   with no router-dependent equipment present.

The configurator is re-run each time the router's configuration needs to be
established or changed, for example at the start of a new season or to
change the team number. It re-detects the router and reapplies configuration
from step 2 above; installation and removal (steps 4 and 6) are then
repeated for each practice session.

For repeated or scripted setup, the same options are available as command
line arguments (`--team`, `--password`, `--wifi-ssid`, `--wifi-password`,
`--allow-ds-wifi`, `--dry-run`, `--debug`; see `--help` for details). The
interactive configurator is the intended mode of use; the arguments exist
primarily to reconfigure a router quickly when the required values are
already known.

## Configuration Reference

For team number `TE-AM` (for example, team 620: `TE=6`, `AM=20`):

| Address | Role |
|---|---|
| `10.TE.AM.1` | VH-109 radio (programmed by the field kiosk) |
| `10.TE.AM.2` | roboRIO |
| `10.TE.AM.3` | Reserved |
| `10.TE.AM.4` | This router |
| `10.TE.AM.5` | Driver Station (static) |
| `10.TE.AM.6`–`.19` | Reserved for other static devices |
| `10.TE.AM.20`–`.199` | DHCP pool (12-hour lease) |

- LAN: `network.lan` is set to the static address and netmask above.
- DHCP: `dhcp.lan` is scoped to start at `.20` with 180 addresses, avoiding
  the reserved and static range. The router hands out its own address as
  both gateway (DHCP option 3) and DNS server (DHCP option 6), which is what
  causes it to serve as the default route for the robot network.
- Wireless access point (optional): `wireless.default_radio0` is set to
  access-point mode with the specified SSID, using WPA2-PSK if a password is
  supplied and open authentication otherwise. This governs devices joining
  the robot network locally through the router and is distinct from the
  router's uplink connection, which is not configured by this script.
- Driver Station wireless control block (enabled by default): installs
  `/etc/frc_ds_block.sh` and registers it as a persistent UCI firewall
  include, so the rule is reapplied on every boot or firewall reload rather
  than applied once. The following are dropped on `wlan0` and `wlan1` only:
  - UDP 1110 (primary Driver Station control channel)
  - UDP 1115 (Driver Station control channel, alternate)
  - TCP 1740 (Driver Station dashboard data stream)

## First-Time Router Setup

If the router has not completed its web-based first-boot wizard, SSH access
is not yet available and the script prints the following instructions:

1. Connect to the router, either over Ethernet or its `GL-SFT1200-xxxx`
   wireless network.
2. Open `http://<router-ip>` (factory default `192.168.8.1`) in a browser.
3. Complete the language and password setup wizard.
4. Re-run the script; it authenticates using the password set in step 3.

## Limitations

- The router is a practice/development aid and is not part of the robot's
  competition configuration. It must be physically removed, along with its
  mounting and wiring, before any official match.
- The script has been tested only against the GL-iNet Opal (GL-SFT1200).
  Other GL-iNet or OpenWrt models will prompt for confirmation before
  proceeding, as `uci` section names may differ between models.
- The router's internet uplink (wireless client or repeater mode) is not
  configured by this script and must be set up separately through the
  GL-iNet web interface.
- Applying the configuration restarts networking, DHCP, and firewall services
  on the router, which changes its address. The SSH session and the
  operator's own network connection are expected to drop briefly as a
  result. The restart is detached so that it completes independently of the
  SSH session, and the script offers to verify reachability at the new
  address afterward.
