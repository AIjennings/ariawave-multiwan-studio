param(
    [string]$RouterIp = "172.30.55.1",
    [string]$LocalBindIp = "172.30.55.235",
    [string]$Username = "cusadmin",
    [Parameter(Mandatory=$true)]
    [string]$Password,
    [string]$Bands5G = "25:41:66:71",
    [string]$BandsLte = "2:4:12:25:41:66:71"
)

$ErrorActionPreference = "Stop"
$cookieFile = "$env:TEMP\hitron_luci_cookie.txt"

Write-Host "[1/5] Authenticating to Hitron D60 ($RouterIp) as '$Username'..." -ForegroundColor Cyan
$loginHeaders = curl.exe -s -i -c $cookieFile --interface $LocalBindIp -X POST "http://$RouterIp/cgi-bin/luci" -d "luci_username=$Username&luci_password=$Password"
$cookieMatch = $loginHeaders | Select-String -Pattern "sysauth_http=([0-9a-fA-F]+)"
if (-not $cookieMatch) {
    throw "Failed to authenticate to http://$RouterIp/cgi-bin/luci"
}
$sid = $cookieMatch.Matches[0].Groups[1].Value
Write-Host "      Authenticated! Session ID: $($sid.Substring(0,8))..." -ForegroundColor Green

function Invoke-Ubus($obj, $method, $argsHash) {
    $b = @{ jsonrpc = "2.0"; id = 1; method = "call"; params = @($sid, $obj, $method, $argsHash) } | ConvertTo-Json -Depth 5 -Compress
    (Invoke-RestMethod -Uri "http://$RouterIp/ubus/" -Method Post -ContentType "application/json" -Body $b).result
}

Write-Host "[2/5] Querying Quectel RG520N-NA RF Signal & Carrier Aggregation..." -ForegroundColor Cyan
$lteRaw = (Invoke-Ubus "file" "exec" @{ command = "/usr/bin/lua"; params = @("/etc/xu_lte/lte_data.lua") })[1].stdout
if ($lteRaw) {
    $lte = $lteRaw | ConvertFrom-Json
    $sc = $lte.Serving_cell
    Write-Host "      Modem: $($lte.ATI) | Mode: $($sc.mode) | Band: $($sc.mode_data.band) ($($sc.mode_data.dl_bandwidth))" -ForegroundColor Yellow
    Write-Host "      Signal: RSRP $($sc.mode_data.rsrp) dBm | SINR $($sc.mode_data.sinr) dB | RSRQ $($sc.mode_data.rsrq) dB" -ForegroundColor Yellow
}

Write-Host "[3/5] Locking 5G SA/NSA ($Bands5G) & 4G LTE ($BandsLte) Bands..." -ForegroundColor Cyan
Invoke-Ubus "file" "exec" @{ command = "/usr/bin/lua"; params = @("/etc/xu_lte/do_at.lua", "set", "bands_5g", $Bands5G) } | Out-Null
Invoke-Ubus "file" "exec" @{ command = "/usr/bin/lua"; params = @("/etc/xu_lte/do_at.lua", "set", "bands_nsa_5g", $Bands5G) } | Out-Null
Invoke-Ubus "file" "exec" @{ command = "/usr/bin/lua"; params = @("/etc/xu_lte/do_at.lua", "set", "bands_lte", $BandsLte) } | Out-Null

Write-Host "[4/5] Disabling DPI NFQUEUE daemons (xu-nfqd, xu_daemon, xu_site_survey, mwan3)..." -ForegroundColor Cyan
foreach ($svc in @("xu-nfqd", "xu_daemon", "xu_site_survey", "mwan3")) {
    Invoke-Ubus "luci" "setInitAction" @{ name = $svc; action = "stop" } | Out-Null
    Invoke-Ubus "luci" "setInitAction" @{ name = $svc; action = "disable" } | Out-Null
}

Write-Host "[5/5] Enabling Qualcomm NSS ECM hardware acceleration & Dual-Core RPS..." -ForegroundColor Cyan
Invoke-Ubus "uci" "delete" @{ config = "firewall"; section = "xu_daemon" } | Out-Null
Invoke-Ubus "uci" "delete" @{ config = "firewall"; section = "xunfqd" } | Out-Null
Invoke-Ubus "uci" "set" @{ config = "firewall"; section = "cfg01e63d"; values = @{ flow_offloading = "1" } } | Out-Null

$fwUser = @"
# Custom performance tuning for Hitron D60 5G Gateway
iptables -t mangle -F xu-nfqd 2>/dev/null
iptables -t mangle -D PREROUTING -i br-lan -j xu-nfqd 2>/dev/null
ip6tables -t mangle -F xu-nfqd 2>/dev/null
ip6tables -t mangle -D PREROUTING -i br-lan -j xu-nfqd 2>/dev/null
iptables -D FORWARD -i br-lan -j xu-intacc-filter 2>/dev/null
ip6tables -D FORWARD -i br-lan -j xu-intacc6-filter 2>/dev/null
iptables -D FORWARD -j xu-cyber-filter 2>/dev/null

# Optimize CPU packet steering (RPS) & NSS ECM acceleration
echo 3 > /sys/class/net/rmnet_mhi0/queues/rx-0/rps_cpus 2>/dev/null
echo 3 > /sys/class/net/rmnet_mhi0.1/queues/rx-0/rps_cpus 2>/dev/null
echo 3 > /sys/class/net/eth1/queues/rx-0/rps_cpus 2>/dev/null
echo 2 > /sys/kernel/debug/ecm/ecm_classifier_default/accel_delay_pkts 2>/dev/null
"@
Invoke-Ubus "file" "write" @{ path = "/etc/firewall.user"; data = $fwUser } | Out-Null
Invoke-Ubus "uci" "commit" @{ config = "firewall" } | Out-Null
Invoke-Ubus "luci" "setInitAction" @{ name = "firewall"; action = "restart" } | Out-Null

Remove-Item $cookieFile -ErrorAction SilentlyContinue
Write-Host "Done! Hitron D60 is tuned for maximum 5G SA + NSS hardware throughput." -ForegroundColor Green
