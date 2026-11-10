# Launch a long StreamSplitter job (Windows) fully detached from the terminal or agent session that starts it.
#
#   powershell -File tools\launch_detached.ps1 -Name get_days -Args "phase0\get_day.py D1 D2 D3"
#
# Win32_Process.Create parents the job outside the caller's console, so closing that console or ending the session
# does not kill it; the job's own console is created hidden, so no window appears that someone could close.
# Logs go to logs\<Name>.log. Phase 1 replaces this with the cross-platform runner.
param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Args
)
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project ".venv\Scripts\python.exe"
$logDir = Join-Path $project "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "$Name.log"
$cmd = "cmd.exe /c `"`"$python`" -u $Args > `"$log`" 2>&1`""
$startup = New-CimInstance -Namespace root/cimv2 -ClassName Win32_ProcessStartup -ClientOnly `
    -Property @{ ShowWindow = [uint16]0 }
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $cmd; CurrentDirectory = $project; ProcessStartupInformation = $startup }
if ($r.ReturnValue -ne 0) { throw "Win32_Process.Create failed with $($r.ReturnValue)" }
$state = [ordered]@{ name = $Name; pid = $r.ProcessId; started_local = (Get-Date -Format "yyyy-MM-dd HH:mm:ss");
                     log = $log; args = $Args }
$state | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $logDir "$Name.launch.json")
"launched $Name pid $($r.ProcessId) -> $log"
