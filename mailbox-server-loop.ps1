# Restart loop for the mailbox server, launched by mailbox-server-loop.bat.
#
# Behaviour (all in the same console window):
#   * Press R or Enter to restart the server now, reloading the code you just
#     edited.
#   * Press Ctrl+C to stop the server and quit the loop immediately, with no
#     "Terminate batch job (Y/N)?" prompt to answer.
#   * If the server exits on its own (crash, or you kill it from Task Manager /
#     Stop-Process) the loop relaunches it with the fresh code after a short
#     delay.
#   * Remote control on loopback (you, or an assistant working with you):
#     POST or GET /api/restart reloads the code; /api/shutdown quits this loop.
#     Helpers: mailbox-restart.cmd and mailbox-stop.cmd.
#
# Environment (set by the .bat, with sensible fallbacks):
#   RELAY_PYTHON         python executable to run
#   RELAY_RESTART_DELAY  seconds to wait between restarts (default 2)

$ErrorActionPreference = 'Stop'
$serverArgs = @($args)

$python = if ($env:RELAY_PYTHON) { $env:RELAY_PYTHON } else { 'python' }
$delaySeconds = 2
if ($env:RELAY_RESTART_DELAY -and (($env:RELAY_RESTART_DELAY -as [int]) -ne $null)) {
    $delaySeconds = [int] $env:RELAY_RESTART_DELAY
}
# Exit code the server returns from POST/GET /api/shutdown to ask us to quit
# rather than relaunch. Keep this in sync with SHUTDOWN_EXIT_CODE in server.py.
$shutdownExitCode = 42

# Deliver Ctrl+C (and other keys) to this loop as ordinary input instead of a
# break signal. That keeps Ctrl+C from reaching cmd.exe (no "Terminate batch
# job" prompt) and lets us react to R/Enter/Ctrl+C while the server runs.
$interactiveConsole = $true
try { $null = [Console]::KeyAvailable } catch { $interactiveConsole = $false }
if ($interactiveConsole) { [Console]::TreatControlCAsInput = $true }

function Get-KeyAction($key) {
    if (($key.Modifiers -band [ConsoleModifiers]::Control) -and
        ($key.Key -eq [ConsoleKey]::C)) { return 'quit' }
    if (($key.Key -eq [ConsoleKey]::R) -or ($key.Key -eq [ConsoleKey]::Enter)) { return 'restart' }
    return 'none'
}

function Read-Action {
    if (-not $interactiveConsole) { return 'none' }
    $action = 'none'
    while ([Console]::KeyAvailable) {
        $result = Get-KeyAction ([Console]::ReadKey($true))
        if ($result -eq 'quit') { return 'quit' }
        if ($result -eq 'restart') { $action = 'restart' }
    }
    return $action
}

function Wait-Action([int] $milliseconds) {
    $elapsed = 0
    while ($elapsed -lt $milliseconds) {
        $action = Read-Action
        if ($action -ne 'none') { return $action }
        Start-Sleep -Milliseconds 120
        $elapsed += 120
    }
    return 'timeout'
}

function ConvertTo-ArgumentString([string[]] $items) {
    ($items | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' '
}

function Start-Server {
    # Launch via System.Diagnostics.Process (not Start-Process) so $server.ExitCode
    # is reported reliably -- that is how /api/shutdown's exit code 42 is detected.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $python
    $psi.Arguments = ConvertTo-ArgumentString (@('-m', 'mailbox_channels.server') + $serverArgs)
    $psi.UseShellExecute = $false
    return [System.Diagnostics.Process]::Start($psi)
}

function Stop-Server($process) {
    if ($process -and -not $process.HasExited) {
        try { $process.Kill() } catch {}
        try { [void] $process.WaitForExit(5000) } catch {}
    }
}

try {
    while ($true) {
        Write-Host '[mailbox-server-loop] starting mailbox server . . . (R/Enter = restart, Ctrl+C = quit)'
        $server = Start-Server

        $action = 'none'
        while (-not $server.HasExited) {
            $action = Read-Action
            if ($action -ne 'none') { break }
            Start-Sleep -Milliseconds 120
        }

        if ($action -eq 'quit') {
            Write-Host '[mailbox-server-loop] Ctrl+C received; stopping the server and exiting.'
            Stop-Server $server
            break
        }

        if ($action -eq 'restart') {
            Write-Host '[mailbox-server-loop] restart requested; reloading the server . . .'
            Stop-Server $server
            continue
        }

        # The server exited on its own: crash, killed to reload, /api/restart, or
        # /api/shutdown (which returns $shutdownExitCode to ask us to quit).
        $code = $null
        try { if ($null -ne $server.ExitCode) { $code = [int] $server.ExitCode } } catch {}
        if ($code -eq $shutdownExitCode) {
            Write-Host '[mailbox-server-loop] shutdown requested (/api/shutdown); quitting loop.'
            break
        }
        $codeText = if ($null -ne $code) { $code } else { 'unknown' }
        Write-Host ("[mailbox-server-loop] server exited (code {0}); restarting in {1}s (R/Enter = now, Ctrl+C = quit) . . ." -f $codeText, $delaySeconds)
        if ((Wait-Action ($delaySeconds * 1000)) -eq 'quit') {
            Write-Host '[mailbox-server-loop] Ctrl+C received; exiting.'
            break
        }
    }
}
finally {
    if ($interactiveConsole) {
        try { [Console]::TreatControlCAsInput = $false } catch {}
    }
}
