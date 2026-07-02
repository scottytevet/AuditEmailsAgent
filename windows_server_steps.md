# Windows Server Public IIS Proxy Steps

These are the next console steps to publish the app through the existing VM DNS:

```text
https://avd-aidev-vm001.eastus.cloudapp.azure.com/
```

The VM already has that hostname bound to the `TranscriptAgent` IIS site. These
steps intentionally reuse that existing HTTPS site and replace what it serves
with a reverse proxy to this Python app on `127.0.0.1:8765`.

Run everything below in **PowerShell as Administrator** on the Windows Server VM.

## 1. Set Variables

Update `$AppPath` if the repo lives somewhere else on the VM.

```powershell
$AppPath = "C:\Users\ScottyGomez\AuditEmailsAgent"
$SiteName = "TranscriptAgent"
$ProxyRoot = "C:\inetpub\AuditEmailsAgentProxy"
$PublicDns = "avd-aidev-vm001.eastus.cloudapp.azure.com"
$PublicUrl = "https://$PublicDns"
$BackendUrl = "http://127.0.0.1:8765"
$appcmd = "$env:windir\System32\inetsrv\appcmd.exe"
```

## 2. Confirm Existing IIS Site Bindings

```powershell
& $appcmd list sites
```

Expected: the `TranscriptAgent` site should have the HTTPS binding for:

```text
avd-aidev-vm001.eastus.cloudapp.azure.com
```

## 3. Back Up IIS Config

```powershell
& $appcmd add backup "before-audit-email-proxy"
```

## 4. Install URL Rewrite

```powershell
$urlRewrite = "$env:TEMP\rewrite_amd64_en-US.msi"

Invoke-WebRequest `
  -Uri "https://download.microsoft.com/download/1/2/8/128E2E22-C1B9-44A4-BE2A-5859ED1D4592/rewrite_amd64_en-US.msi" `
  -OutFile $urlRewrite

Start-Process msiexec.exe -ArgumentList "/i `"$urlRewrite`" /qn /norestart" -Wait
```

Verify:

```powershell
Test-Path "$env:windir\System32\inetsrv\rewrite.dll"
```

Expected:

```text
True
```

## 5. Install Application Request Routing

```powershell
$arr = "$env:TEMP\requestRouter_amd64.msi"

Invoke-WebRequest `
  -Uri "https://go.microsoft.com/fwlink/?LinkID=615136" `
  -OutFile $arr

Start-Process msiexec.exe -ArgumentList "/i `"$arr`" /qn /norestart" -Wait
```

Verify:

```powershell
Test-Path "C:\Program Files\IIS\Application Request Routing\requestRouter.dll"
```

Expected:

```text
True
```

## 6. Enable ARR Reverse Proxy

```powershell
& $appcmd set config -section:system.webServer/proxy /enabled:"True" /preserveHostHeader:"True" /commit:apphost
```

Verify:

```powershell
& $appcmd list config -section:system.webServer/proxy
```

Look for:

```xml
<proxy enabled="true" preserveHostHeader="true" />
```

## 7. Configure The Python App For Localhost Behind IIS

```powershell
cd $AppPath

(Get-Content .env) `
  -replace '^EMAIL_MVP_HOST=.*', 'EMAIL_MVP_HOST=127.0.0.1' `
  -replace '^EMAIL_MVP_PORT=.*', 'EMAIL_MVP_PORT=8765' `
  -replace '^ACCESS_CONTROL_PUBLIC_BASE_URL=.*', "ACCESS_CONTROL_PUBLIC_BASE_URL=$PublicUrl" |
  Set-Content .env
```

For temporary testing without Microsoft sign-in, make sure this is set in `.env`:

```env
ACCESS_CONTROL_ENABLED=false
```

For production with Microsoft sign-in, use:

```env
ACCESS_CONTROL_ENABLED=true
ACCESS_CONTROL_MODE=oidc
ACCESS_CONTROL_PUBLIC_BASE_URL=https://avd-aidev-vm001.eastus.cloudapp.azure.com
```

and make sure the Entra app redirect URI includes:

```text
https://avd-aidev-vm001.eastus.cloudapp.azure.com/signin-oidc
```

## 8. Make Sure The Python App Is Running

If running manually:

```powershell
cd $AppPath
.\.venv\Scripts\Activate.ps1
python -m app.server
```

If using the scheduled task:

```powershell
Stop-ScheduledTask -TaskName AuditEmailsAgent -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName AuditEmailsAgent
```

If the scheduled task does not exist yet:

```powershell
$python = Join-Path $AppPath ".venv\Scripts\python.exe"
$action = New-ScheduledTaskAction -Execute $python -Argument "-m app.server" -WorkingDirectory $AppPath
$trigger = New-ScheduledTaskTrigger -AtStartup

Register-ScheduledTask -TaskName AuditEmailsAgent -Action $action -Trigger $trigger -RunLevel Highest -Force
Start-ScheduledTask -TaskName AuditEmailsAgent
```

## 9. Create The IIS Proxy Folder And web.config

```powershell
New-Item -ItemType Directory -Force $ProxyRoot | Out-Null

@"
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <rule name="AuditEmailsAgentReverseProxy" stopProcessing="true">
          <match url="(.*)" />
          <action type="Rewrite" url="http://127.0.0.1:8765/{R:1}" appendQueryString="true" />
        </rule>
      </rules>
    </rewrite>
  </system.webServer>
</configuration>
"@ | Set-Content "$ProxyRoot\web.config" -Encoding UTF8
```

## 10. Point The Existing HTTPS Site To The Proxy Folder

This replaces what the existing `TranscriptAgent` site serves.

```powershell
Import-Module WebAdministration
Set-ItemProperty "IIS:\Sites\$SiteName" -Name physicalPath -Value $ProxyRoot
iisreset
```

## 11. Test From The VM

Test backend directly:

```powershell
Invoke-WebRequest http://127.0.0.1:8765/api/status -UseBasicParsing
```

Test through IIS over HTTP:

```powershell
Invoke-WebRequest http://localhost/api/status -UseBasicParsing
```

Test through public HTTPS binding:

```powershell
Invoke-WebRequest https://avd-aidev-vm001.eastus.cloudapp.azure.com/api/status -UseBasicParsing
```

Then open in a browser:

```text
https://avd-aidev-vm001.eastus.cloudapp.azure.com/
```

## 12. If Public Browser Does Not Work

Check Windows Firewall:

```powershell
New-NetFirewallRule -DisplayName "HTTPS 443" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
New-NetFirewallRule -DisplayName "HTTP 80" -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow
```

Check Azure networking:

```text
Azure Portal -> VM -> Networking -> Network security group
```

Make sure inbound `443` is allowed. Allow inbound `80` only if you still need
HTTP redirect/testing.

Check IIS site state:

```powershell
& $appcmd list sites
& $appcmd list apppools
```

Check Python backend is listening:

```powershell
netstat -ano | findstr ":8765"
```

## 13. Roll Back IIS If Needed

If the reverse proxy breaks the existing site and you need to restore the IIS
config backup:

```powershell
& $appcmd restore backup "before-audit-email-proxy"
iisreset
```

If you only need to point the `TranscriptAgent` site back to its old folder, use
the folder from the IIS backup or from:

```powershell
Get-Website | Format-Table Name,PhysicalPath,Bindings -AutoSize
```

Then set it back:

```powershell
Set-ItemProperty "IIS:\Sites\$SiteName" -Name physicalPath -Value "C:\OLD\TRANSCRIPT\AGENT\PATH"
iisreset
```
