---
name: ariawave-multiwan-tuner
description: >-
  Use this skill to audit, speedtest, tune, or bond up to 4 concurrent 5G cellular / Ethernet WAN
  connections on an Ariawave rig: Hitron D60 (R5Q-D60-1G-DB4-US / Quectel RG520N-NA OpenWrt/LuCI ubus tuning),
  Inseego MiFi M3000/M3100 (USB RNDIS), Inseego MiFi M2000/M2100 (USB Ethernet Dock), and Apple iPhone 5G USB
  Tether. Handles Windows IP_UNICAST_IF (socket option 31) pre-connect hardware interface binding to prevent
  192.168.1.1 gateway subnet collisions, and runs striped work-stealing HTTP Range downloads via multi_wan.py.
---

# Ariawave Quad-WAN Sovereign Cellular Bonder & RF Tuner

## 1. The 4-Connection Hardware Topology

| Slot | Windows Adapter | Hardware Device | Interface / Link | Local Subnet & Gateway | Typical Speed (DL / UL) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **WAN 1** | `Ethernet` (`ifIndex 12`) | **Hitron D60 (`R5Q-D60-1G-DB4-US`)** w/ Quectel `RG520N-NA` | Realtek PCIe GbE (1 Gbps) | `172.30.55.235/24` → `172.30.55.1` | **243.86 / 13.33 Mbps** (NR5G-SA Band `n41` 80MHz) |
| **WAN 2** | `Ethernet 4` (`ifIndex 15`) | **Apple iPhone 5G USB Tether** | Apple Mobile Device Ethernet (1.1 Gbps) | `172.20.10.8/28` → `172.20.10.1` | **276.65 / 24.38 Mbps** (T-Mobile 5G UC) |
| **WAN 3** | `Ethernet 5` (`ifIndex 25`) | **Inseego MiFi X PRO 5G (`M3000/M3100`)** | Remote NDIS USB-C Direct (426 Mbps) | `192.168.1.44/24` → `192.168.1.1` | **144.10 / 23.87 Mbps** (5G NSA EN-DC) |
| **WAN 4** | `Ethernet 2` (`ifIndex 18`) | **Inseego 5G MiFi (`M2000/M2100`)** | USB 3.0 5K Dock GbE (1 Gbps) | `192.168.1.2/24` → `192.168.1.1` | **116.47 / 42.27 Mbps** (5G NSA EN-DC) |

* **Combined Peak Single-Link Sum:** **781.08 Mbps Down / 103.85 Mbps Up**
* **Sustained Single-File Striped Throughput (1.15 GB payload):** **352.13 Mbps (41.98 MB/s)**

---

## 2. Quick Audit & Per-Interface Speedtest Commands

### Verify all 4 WANs and WAN IPs:
```powershell
python C:\Ariawave\aria_gaming_os\multi_wan.py --status
```

### Run individual Ookla CLI speedtests bound to each local IP:
```powershell
foreach ($ip in @("172.30.55.235", "172.20.10.8", "192.168.1.44", "192.168.1.2")) {
    Write-Output "=== Testing $ip ==="
    speedtest.exe --accept-license --accept-gdpr -i $ip -p no
}
```

### Run a 4-WAN Bonded Striped Benchmark:
```powershell
Set-Location C:\Ariawave\aria_gaming_os
python -c "import multi_wan, tempfile, os, json; f=os.path.join(tempfile.gettempdir(),'mw_bench.bin'); res=multi_wan.download_file_multi_wan('https://dl.google.com/go/go1.22.0.windows-amd64.zip', f, chunk_size_bytes=4*1024*1024, workers_per_nic=4); print(json.dumps(res, indent=2)); os.remove(f)"
```

---

## 3. Solving the Dual-Inseego `192.168.1.1` Gateway Collision

Both the Inseego **M2000** (`Ethernet 2`, `192.168.1.2`) and **M3000** (`Ethernet 5`, `192.168.1.44`) use `192.168.1.1/24` as their gateway.
- Calling `socket.bind(('192.168.1.44', 0))` alone on Windows is **not enough**: Windows still consults the routing table for `0.0.0.0/0` and routes packets out whichever `192.168.1.1` interface has the lower metric (`Ethernet 2`), causing TCP resets when the source IP doesn't match the NAT state on the other modem.
- **The Fix (`multi_wan.py`):** Set Windows socket option `IP_UNICAST_IF` (`31` on `IPPROTO_IP`) with `struct.pack("!I", if_index)` **before** calling `sock.connect()`:
  ```python
  sock.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", int(if_index)))
  sock.bind((source_ip, 0))
  sock.connect(sa)
  ```

---

## 4. Hitron D60 (`172.30.55.1`) OpenWrt / Quectel RG520N-NA Tuning Playbook

The Hitron D60 (`R5Q-D60-1G-DB4-US`) runs OpenWrt 3.3.9 (`IPQ5018`) with a Quectel `RG520N-NA` modem on `rmnet_mhi0.1` (`pcie_mhi` QMAP driver).
1. **Authenticate via LuCI (`http://172.30.55.1/cgi-bin/luci`):**
   - Default username: `cusadmin`
   - Call JSON-RPC at `http://172.30.55.1/ubus/` using the `sysauth_http` session ID.
2. **Check RF Signal (`RSRP`, `SINR`, `RSRQ`, Active Band & Carrier Aggregation):**
   - Execute `/usr/bin/lua /etc/xu_lte/lte_data.lua` via `ubus` `file.exec`.
3. **Lock 5G SA/NSA & LTE Bands to T-Mobile North America:**
   - 5G SA (`bands_5g`): `25:41:66:71`
   - 5G NSA (`bands_nsa_5g`): `25:41:66:71`
   - 4G LTE (`bands_lte`): `2:4:12:25:41:66:71`
4. **Hardware Offloading (`qca-nss-ecm`) & DPI Bypass:**
   - Stop & disable `xu-nfqd`, `xu_daemon`, `xu_site_survey`, and `mwan3` (removes `NFQUEUE` queue 100/101 overhead on ports 80/443/53).
   - Persist in `/etc/firewall.user`:
     ```sh
     echo 3 > /sys/class/net/rmnet_mhi0/queues/rx-0/rps_cpus
     echo 3 > /sys/class/net/rmnet_mhi0.1/queues/rx-0/rps_cpus
     echo 3 > /sys/class/net/eth1/queues/rx-0/rps_cpus
     echo 2 > /sys/kernel/debug/ecm/ecm_classifier_default/accel_delay_pkts
     ```
