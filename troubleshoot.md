Troubleshooting Public IIS Proxy 500
Run all commands in PowerShell as Administrator on the VM.
Problem
This works:
Invoke-WebRequest http://127.0.0.1:8765/api/status -UseBasicParsing
But public DNS returns 500:
Invoke-WebRequest https://avd-aidev-vm001.eastus.cloudapp.azure.com/api/status -UseBasicParsing
That means the Python app is healthy. The issue is likely IIS, URL Rewrite, ARR, or the IIS site path.
Commands
$SiteName = "TranscriptAgent"
$ProxyRoot = "C:\inetpub\AuditEmailsAgentProxy"
$appcmd = "$env:windir\System32\inetsrv\appcmd.exe"

Import-Module WebAdministration

Get-Website | Select-Object Name,State,PhysicalPath,@{
  Name="Bindings"
  Expression={$_.Bindings.Collection.bindingInformation -join "; "}
} | Format-List

Set-ItemProperty "IIS:\Sites\$SiteName" -Name physicalPath -Value $ProxyRoot
iisreset

Get-Content C:\inetpub\AuditEmailsAgentProxy\web.config

Test-Path "$env:windir\System32\inetsrv\rewrite.dll"
Test-Path "C:\Program Files\IIS\Application Request Routing\requestRouter.dll"

& $appcmd list config -section:system.webServer/proxy

& $appcmd set config -section:system.webServer/proxy /enabled:"True" /preserveHostHeader:"True" /commit:apphost
iisreset

curl.exe -vk https://avd-aidev-vm001.eastus.cloudapp.azure.com/api/status

Get-ChildItem C:\inetpub\logs\LogFiles -Recurse -Filter *.log |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 |
  Get-Content -Tail 50

Get-EventLog -LogName Application -Newest 20 |
  Where-Object { $_.Source -like "*IIS*" -or $_.Source -like "*WAS*" -or $_.Source -like "*ASP*" } |
  Format-List TimeGenerated,Source,EntryType,Message
Expected Checks
TranscriptAgent should show:
PhysicalPath : C:\inetpub\AuditEmailsAgentProxy
These should both return True:
Test-Path "$env:windir\System32\inetsrv\rewrite.dll"
Test-Path "C:\Program Files\IIS\Application Request Routing\requestRouter.dll"
ARR proxy config should include:
<proxy enabled="true" preserveHostHeader="true" />
web.config should include:
<action type="Rewrite" url="http://127.0.0.1:8765/{R:1}" appendQueryString="true" />
Azure Ports
For IIS reverse proxy, Azure only needs:
TCP 443
TCP 80 optional
Do not open 8765 for production. IIS talks to the Python app locally:
Internet -> Azure NSG 443 -> IIS -> 127.0.0.1:8765

