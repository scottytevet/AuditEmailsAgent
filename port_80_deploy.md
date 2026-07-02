# Windows Server Port 80 IIS Proxy Steps

Use this only if HTTPS on port `443` is blocked by Azure permissions and you
want to test public access over HTTP port `80`.

Run all commands in **PowerShell as Administrator** on the VM.

## Important Notes

- Port `80` also needs to be allowed in the Azure NSG. If you cannot change NSG
  rules, public HTTP may be blocked too.
- The VM already has an IIS site on port `80` named:

```text
TranscriptAgent HTTP Redirect
```

- These steps reuse that existing port `80` IIS site and point it to the same
  reverse proxy folder used by the app.
- For production sign-in, HTTPS is strongly preferred. HTTP is only a temporary
  test path.

## 1. Set Variables

```powershell
$SiteName = "TranscriptAgent HTTP Redirect"
$ProxyRoot = "C:\inetpub\AuditEmailsAgentProxy"
$PublicDns = "avd-aidev-vm001.eastus.cloudapp.azure.com"
$appcmd = "$env:windir\System32\inetsrv\appcmd.exe"
```

## 2. Point The Port 80 Site To The Proxy Folder

```powershell
Import-Module WebAdministration

Set-ItemProperty "IIS:\Sites\$SiteName" -Name physicalPath -Value $ProxyRoot
```

## 3. Make Sure ARR Proxy Is Enabled

```powershell
& $appcmd set config -section:system.webServer/proxy /enabled:"True" /preserveHostHeader:"True" /commit:apphost
```

## 4. Restart IIS

```powershell
iisreset
```

## 5. Allow Port 80 In Windows Firewall

```powershell
New-NetFirewallRule -DisplayName "HTTP 80" -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow
```

If the rule already exists, PowerShell may show an error. That is usually fine.

## 6. Test Locally With The Real Host Header

```powershell
curl.exe -v --resolve "$PublicDns`:80:127.0.0.1" "http://$PublicDns/api/status"
```

Expected result:

```text
HTTP/1.1 200 OK
```

and JSON from the app status endpoint.

## 7. Test From A Public Browser

Open this from your laptop or another machine outside the VM:

```text
http://avd-aidev-vm001.eastus.cloudapp.azure.com/
```

If this times out, Azure NSG port `80` is probably blocked.

## 8. Azure NSG Rule Needed For Public HTTP

Someone with Azure permissions must add or confirm this rule:

```text
Name: Allow-HTTP-80
Source: Any
Source port ranges: *
Destination: Any
Service: HTTP
Destination port ranges: 80
Protocol: TCP
Action: Allow
Priority: 1010
```

## 9. Auth Warning For HTTP

If Microsoft sign-in is enabled, HTTP may fail because secure cookies require
HTTPS.

For plain HTTP testing, use this in `.env`:

```env
ACCESS_CONTROL_ENABLED=false
```

If you later switch back to HTTPS production, restore:

```env
ACCESS_CONTROL_ENABLED=true
ACCESS_CONTROL_MODE=oidc
ACCESS_CONTROL_PUBLIC_BASE_URL=https://avd-aidev-vm001.eastus.cloudapp.azure.com
```

## 10. Keep Port 8765 Private

Do not open `8765` for the IIS proxy setup. IIS talks to the Python app locally:

```text
Internet -> Azure NSG 80 -> IIS -> 127.0.0.1:8765
```

