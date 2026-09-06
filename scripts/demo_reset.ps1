# One command to get to a clean demo. Reseeding under a live backend is the trap: uvicorn holds
# an open handle to the SQLite file, so the reseed writes to a database the server is no longer
# reading -- which surfaces in the browser as "Order not found", and, if an earlier rehearsal
# confirmed a booking, as a stop sitting 0.0km from the customer being offered a slot.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -like "*uvicorn*dispatch_agent*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 2

& $python (Join-Path $root "scripts\reset_demo.py") --yes

Start-Process -FilePath $python -WorkingDirectory $root -WindowStyle Minimized `
    -ArgumentList "-m", "uvicorn", "dispatch_agent.webapp.main:app", "--port", "8000", "--log-level", "warning"

# A TCP probe, not an HTTP one: Invoke-WebRequest goes through the machine's proxy settings and
# reported "did not come up" against a server that was already listening.
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 1
    $probe = New-Object Net.Sockets.TcpClient
    try {
        $probe.Connect("127.0.0.1", 8000)
        $probe.Close()
        Write-Host "Ready. Seeded routes published, backend listening on :8000."
        exit 0
    } catch { } finally { $probe.Dispose() }
}
Write-Error "Backend did not come up within 60s."
