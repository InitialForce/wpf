#!/usr/bin/env powershell
# Long-running display-keepalive. eval.py spawns this during the
# swap → spike → restore window. Killed when eval.py exits or a new
# eval starts. SetThreadExecutionState lasts as long as the calling
# thread is alive; an idle Start-Sleep loop keeps that thread alive.
#
# Flag values (uint32):
#   0x80000000 ES_CONTINUOUS         — keep flags set until next call
#   0x00000002 ES_DISPLAY_REQUIRED   — prevent monitor sleep
#   0x00000001 ES_SYSTEM_REQUIRED    — prevent system sleep / standby
#   Combined: 0x80000003

$signature = @'
using System;
using System.Runtime.InteropServices;
public static class P {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint esFlags);
}
'@
Add-Type -TypeDefinition $signature -PassThru | Out-Null
[void][P]::SetThreadExecutionState(0x80000003)
Write-Host "[keep-display-awake] holding ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED | ES_CONTINUOUS (PID $PID)"

# Spin slowly so the thread stays alive but uses ~0% CPU.
while ($true) { Start-Sleep -Seconds 60 }
