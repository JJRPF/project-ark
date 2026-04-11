# ROUTER_SETUP.md — Asus Captive Portal for Project Ark

> This document walks you through turning a supported Asus router into an **open, offline captive portal** that forces every connected client into the Project Ark web app running on your Raspberry Pi 5.
>
> **This step cannot be automated.** Router flashing is risky, vendor-specific, and must be done by hand. Read everything before you do anything.

---

## 0. What You Are Building

```
 [Phone / tablet]  ──open Wi-Fi──▶  [Asus router]  ──LAN──▶  [Raspberry Pi 5]
                                       │
                                       │  NoDogSplash captive portal
                                       │  redirects ALL HTTP to Pi:80
                                       ▼
                                 DHCP + DNS hijack
```

Key properties:
- **No WAN.** The router must not be connected to the internet.
- **Open SSID.** No password — this is a public emergency node.
- **Client isolation.** Clients cannot see each other.
- **Captive portal.** Every HTTP request is intercepted and sent to the Pi's static IP.
- **DNS hijack.** Every DNS query resolves to the Pi, so even typed URLs land on Ark.

---

## 1. Supported Hardware

Any Asus router that can run **DD-WRT** or **FreshTomato** and that has ≥ 8 MB flash and ≥ 64 MB RAM. Tested models:

| Model | Firmware | Notes |
|---|---|---|
| Asus RT-AC68U | FreshTomato 2024.x | Recommended. Cheap, plentiful, strong radio. |
| Asus RT-N66U | DD-WRT v3.0-r50xxx | Older but stable. |
| Asus RT-AC66U B1 | FreshTomato 2024.x | Works identically to AC68U. |
| Asus RT-AX88U | DD-WRT (beta) | Only if you know what you're doing. |

Always cross-check your **exact hardware revision** against:
- DD-WRT router database: <https://dd-wrt.com/support/router-database/>
- FreshTomato compatibility list: <https://freshtomato.org/>

> ⚠️ **Flashing the wrong image will brick your router.** There is no warranty here.

---

## 2. Flashing the Firmware

### Option A — FreshTomato (recommended for AC68U / AC66U B1)

1. Download the correct build for **your exact model** from <https://freshtomato.org/downloads/>.
   - Use the `*_AIO-64K.trx` or `*_AIO.trx` build that matches your flash size.
2. Power-cycle the router into recovery mode:
   - Unplug power.
   - Hold the **WPS** button (or **Reset** on some models).
   - Plug power back in while holding the button for ~10 seconds until the power LED blinks slowly.
3. Connect your computer directly to a **LAN port** on the router (not WAN). Give your NIC a static IP of `192.168.1.2/24`.
4. Open the Asus Firmware Restoration Utility (Windows) or use `tftp` (Linux/macOS):
   ```bash
   tftp 192.168.1.1
   tftp> binary
   tftp> rexmt 1
   tftp> timeout 60
   tftp> put freshtomato-RT-AC68U-...trx
   ```
5. Wait **at least 5 minutes** after the transfer completes. Do not unplug. The router will reboot on its own.
6. Browse to `http://192.168.1.1`. Default creds are usually `root` / `admin`. **Change them immediately.**

### Option B — DD-WRT

1. Find your model on the DD-WRT router database and download the **factory-to-dd-wrt** image plus the matching **generic** upgrade image.
2. Flash the factory image via the Asus stock web UI's firmware upgrade page.
3. After first boot, set a new admin password.
4. Flash the generic image on top of the factory image via DD-WRT's Administration → Firmware Upgrade page. **Reset to defaults** after flashing.

---

## 3. Base Network Configuration

All of this is done in the router's web UI after flashing.

1. **Disable the WAN**:
   - `Basic → Network → WAN`: set **WAN Type = Disabled**.
   - Physically unplug any cable from the WAN port.
2. **Set the LAN**:
   - Router IP: `192.168.1.1`
   - Subnet: `255.255.255.0`
   - DHCP range: `192.168.1.100 – 192.168.1.200`
   - **Reserve** `192.168.1.50` for the Raspberry Pi (by MAC address).
3. **Wireless (2.4 GHz)**:
   - SSID: `Project-Ark` (or whatever you want)
   - Security: **Open / None**
   - Broadcast SSID: **Yes**
   - **AP Isolation / Client Isolation**: **Enabled**
4. **Wireless (5 GHz)**:
   - Same SSID, same open config, same isolation.
5. **Disable remote admin, UPnP, WPS, and any cloud services.** This is an offline device.

Save and reboot.

---

## 4. Assign the Raspberry Pi a Static IP

Plug the Pi into a **LAN port** via Ethernet. Either:

- **DHCP reservation** (preferred): in the router UI, reserve `192.168.1.50` for the Pi's MAC address.
- **Static on the Pi**: edit `/etc/dhcpcd.conf` on the Pi:
  ```
  interface eth0
  static ip_address=192.168.1.50/24
  static routers=192.168.1.1
  static domain_name_servers=192.168.1.1
  ```

Reboot the Pi and confirm: `ping 192.168.1.50` from the router or another LAN client.

---

## 5. Install NoDogSplash (Captive Portal)

FreshTomato and DD-WRT both support **JFFS** (persistent storage) and shell access. Install NoDogSplash from Entware or compile it if your firmware does not bundle it.

### Enable JFFS + SSH

- `Administration → JFFS2`: **Enable**, **Format**, **Save**. Reboot.
- `Administration → SSH Daemon`: **Enable**, port 22, password login. Save.

### Install Entware

SSH in as `root@192.168.1.1` and run:

```sh
mkdir -p /jffs/opt
mount -o bind /jffs/opt /opt
wget -O - http://bin.entware.net/aarch64-k3.10/installer/generic.sh | sh
```

(Use `armv7sf-k3.2` instead for 32-bit Asus models.)

### Install NoDogSplash

```sh
opkg update
opkg install nodogsplash
```

---

## 6. Configure NoDogSplash

Edit `/opt/etc/nodogsplash/nodogsplash.conf`:

```conf
GatewayInterface br0
GatewayAddress   192.168.1.1
MaxClients       250
ClientIdleTimeout 480
ClientForceTimeout 1440

# Where the captive portal "splash" page lives —
# we redirect everything to the Pi instead of serving a local page.
RedirectURL http://192.168.1.50/

# Allow traffic to the Pi on 80 and 8080 (Kiwix) without auth.
FirewallRuleSet preauthenticated-users {
    FirewallRule allow tcp port 80  to 192.168.1.50
    FirewallRule allow tcp port 8080 to 192.168.1.50
    FirewallRule allow udp port 53  to 192.168.1.1
    FirewallRule block all
}

FirewallRuleSet authenticated-users {
    FirewallRule allow tcp port 80  to 192.168.1.50
    FirewallRule allow tcp port 8080 to 192.168.1.50
    FirewallRule block all
}

# Auto-authenticate every client — no login wall, just redirect.
AuthIdleTimeout 0
```

Enable and start it:

```sh
/opt/etc/init.d/S50nodogsplash enable
/opt/etc/init.d/S50nodogsplash start
```

Make it start on boot by adding this to the router's **startup script** (`Administration → Scripts → Init`):

```sh
sleep 15
/opt/etc/init.d/S50nodogsplash start
```

---

## 7. DNS Hijack (Optional but Strongly Recommended)

Without this, clients typing `https://google.com` will just see a connection error. We want *any* hostname to resolve to the Pi.

In `Administration → Scripts → Firewall` on FreshTomato:

```sh
iptables -t nat -A PREROUTING -i br0 -p udp --dport 53 -j DNAT --to-destination 192.168.1.50:53
iptables -t nat -A PREROUTING -i br0 -p tcp --dport 53 -j DNAT --to-destination 192.168.1.50:53
```

On the Pi, run a tiny DNS responder (e.g. `dnsmasq` with `address=/#/192.168.1.50`) so every query → Pi.

**Note:** HTTPS sites will still show certificate errors (you are not Google), but that's fine — the whole point is that users land on Ark.

---

## 8. Verification Checklist

From a phone:

- [ ] You can see the `Project-Ark` SSID and connect without a password.
- [ ] Your phone shows a "Sign in to Wi-Fi network" notification.
- [ ] Tapping the notification opens the Project Ark portal.
- [ ] Typing `http://neverssl.com` in a browser also lands on Ark.
- [ ] Submitting a query returns a bulleted answer within ~10–30 seconds.
- [ ] Other clients on the same SSID cannot see each other.

From the Pi (SSH from the router LAN):

- [ ] `sudo systemctl status ark-flask` → `active (running)`
- [ ] `sudo systemctl status ark-kiwix` → `active (running)`
- [ ] `curl http://127.0.0.1/` returns the Ark HTML.
- [ ] `curl http://127.0.0.1:8080/search?pattern=water&books.name=wikipedia_en_all` returns JSON/HTML.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Phone connects but no captive popup | OS captive detection disabled | Open a browser and go to `http://neverssl.com` manually. |
| Portal times out | Pi has wrong IP / service down | `ping 192.168.1.50` from router, check `ark-flask` status. |
| Kiwix returns 404 | Wrong ZIM name in systemd unit | `sudo systemctl edit ark-kiwix` and fix the path. |
| Ollama replies are empty | Model not pulled or OOM | `ollama list` on Pi; pull again; watch `dmesg` for OOM kills. |
| Router reboots under load | PSU or thermal | Use the stock Asus PSU; add airflow. |

---

## 10. Do Not Do This If...

- …you are not comfortable recovering a bricked router via TFTP.
- …you are deploying on an active network with other users on it.
- …you are in a jurisdiction that regulates open Wi-Fi broadcasts.

Project Ark is designed for **disaster-response, off-grid, or educational** deployments. Use it accordingly.
