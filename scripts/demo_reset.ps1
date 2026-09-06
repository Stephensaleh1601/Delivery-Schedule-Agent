# One command to get to a clean demo: stop the backend, rebuild the database, start it again.
#
# Reseeding under a live backend is the trap this exists to close. uvicorn holds an open handle to
# the SQLite file, so the reseed writes to a database the server is no longer reading -- which
# surfaces in the browser as "Order not found", and, if an earlier rehearsal confirmed a booking,
# as a stop sitting 0.0km from the customer being offered a slot.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

function Test-PortBusy {
    $probe = New-Object Net.Sockets.TcpClient
    try { $probe.Connect("127.0.0.1", 8000); $probe.Close(); return $true }
    catch { return $false }
    finally { $probe.Dispose() }
}

Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -like "*uvicorn*dispatch_agent*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# Whether the port ACTUALLY freed, not whether we asked it to. A backend started from another
# terminal, or by another user session, is not ours to stop -- and the failure is silent and
# vicious: the new uvicorn cannot bind and exits, the old one keeps serving, and a probe against
# :8000 succeeds. The script then reports "Ready" while every code change since that server
# started is invisible. That cost an hour of chasing a bug that was already fixed.
if (Test-PortBusy) {
    Write-Host ""
    Write-Host "Port 8000 is still in use by a backend this script cannot stop." -ForegroundColor Yellow
    Write-Host "It was almost certainly started in another terminal window." -ForegroundColor Yellow
    Write-Host "Close that window (Ctrl+C), then run this script again." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Nothing was reseeded -- the database is untouched." -ForegroundColor Yellow
    exit 1
}

& $python (Join-Path $root "scripts\reset_demo.py") --yes

Start-Process -FilePath $python -WorkingDirectory $root -WindowStyle Minimized `
    -ArgumentList "-m", "uvicorn", "dispatch_agent.webapp.main:app", "--port", "8000", "--log-level", "warning"

# A TCP probe, not an HTTP one: Invoke-WebRequest goes through the machine's proxy settings and
# reported "did not come up" against a server that was already listening.
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 1
    if (Test-PortBusy) {
        Write-Host "Ready. Seeded routes published, backend listening on :8000."
        exit 0
    }
}
Write-Error "Backend did not come up within 60s."
