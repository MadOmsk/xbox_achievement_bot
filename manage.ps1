<#
.SYNOPSIS
    Start, stop and inspect the bot process on this machine.

.DESCRIPTION
    The bot cannot start itself, so process control lives outside it.
    On a real server this job belongs to systemd; this script is its
    equivalent for a development machine.

.EXAMPLE
    .\manage.ps1 start
    .\manage.ps1 status
    .\manage.ps1 logs -Lines 50
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs')]
    [string]$Command = 'status',

    [int]$Lines = 20
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'
$LogFile = Join-Path $LogDir 'bot.log'
$ErrFile = Join-Path $LogDir 'bot.err.log'
$PidFile = Join-Path $Root 'data\bot.pid'
$Port = 8080

function Get-BotProcess {
    # Trust the PID file only after confirming the process is still ours:
    # PIDs get reused, and killing a stranger would be worse than not stopping.
    if (-not (Test-Path $PidFile)) { return $null }
    $recorded = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $recorded) { return $null }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$recorded" -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    if ($process.CommandLine -notmatch 'bot\.main') { return $null }
    return $process
}

function Get-BotTree {
    # One launch is two processes on Windows: .venv\Scripts\python.exe starts
    # the base interpreter as a child. Both must count as "ours", or the child
    # looks like a second bot and owns the port we think is free.
    param($Process)
    if (-not $Process) { return @() }
    $ids = @($Process.ProcessId)
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($Process.ProcessId)" -ErrorAction SilentlyContinue
    foreach ($child in $children) { $ids += $child.ProcessId }
    return $ids
}

function Start-Bot {
    if (Get-BotProcess) { Write-Host 'Бот уже работает.' -ForegroundColor Yellow; return }
    if (-not (Test-Path $Python)) { throw "Нет окружения: $Python. Создай: python -m venv .venv" }
    if (-not (Test-Path (Join-Path $Root '.env'))) { throw 'Нет .env в корне проекта.' }

    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $PidFile) | Out-Null
    # Keep the previous run readable: Start-Process truncates its target.
    if (Test-Path $LogFile) { Move-Item $LogFile (Join-Path $LogDir 'bot.prev.log') -Force }

    $process = Start-Process -FilePath $Python -ArgumentList '-u', '-m', 'bot.main' `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $LogFile -RedirectStandardError $ErrFile
    $process.Id | Set-Content $PidFile -Encoding ascii

    Start-Sleep -Seconds 3
    if (Get-BotProcess) {
        Write-Host "Запустил, PID $($process.Id). Логи: logs\bot.log" -ForegroundColor Green
    } else {
        Write-Host 'Процесс не удержался. Последние строки ошибок:' -ForegroundColor Red
        if (Test-Path $ErrFile) { Get-Content $ErrFile -Tail 15 }
    }
}

function Stop-Bot {
    $process = Get-BotProcess
    if (-not $process) { Write-Host 'Бот не работает.' -ForegroundColor Yellow; return }
    foreach ($id in (Get-BotTree $process)) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Write-Host "Остановил PID $($process.ProcessId)." -ForegroundColor Green
}

function Show-Status {
    $process = Get-BotProcess
    if ($process) {
        $started = $process.CreationDate
        $uptime = (Get-Date) - $started
        Write-Host 'Бот работает' -ForegroundColor Green
        Write-Host ("  PID:      {0}" -f $process.ProcessId)
        Write-Host ("  запущен:  {0:HH:mm:ss}, аптайм {1:hh\:mm\:ss}" -f $started, $uptime)
    } else {
        Write-Host 'Бот не работает' -ForegroundColor Red
    }

    $tree = Get-BotTree $process
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        $mine = if ($tree -contains $listener.OwningProcess) { ', наш' } else { ', ЧУЖОЙ' }
        Write-Host ("  порт {0}: слушает (PID {1}{2})" -f $Port, $listener.OwningProcess, $mine)
    } else {
        Write-Host ("  порт {0}: свободен" -f $Port)
    }

    # A second bot with the same token would fight this one for Telegram updates.
    $others = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'bot\.main' -and $tree -notcontains $_.ProcessId })
    if ($others.Count -gt 0) {
        Write-Host ("  ВНИМАНИЕ: посторонние процессы бота: {0}" -f
            ($others.ProcessId -join ', ')) -ForegroundColor Yellow
        Write-Host '  Два бота с одним токеном конфликтуют в Telegram.' -ForegroundColor Yellow
    }

    if (Test-Path $Python) {
        Write-Host ''
        & $Python (Join-Path $Root 'scripts\db_status.py')
    }

    if (Test-Path $LogFile) {
        Write-Host ''
        Write-Host "Последние строки лога:" -ForegroundColor Cyan
        Get-Content $LogFile -Tail 5
    }
}

switch ($Command) {
    'start'   { Start-Bot }
    'stop'    { Stop-Bot }
    'restart' { Stop-Bot; Start-Sleep -Seconds 1; Start-Bot }
    'status'  { Show-Status }
    'logs'    {
        if (-not (Test-Path $LogFile)) { Write-Host 'Логов пока нет.'; break }
        Get-Content $LogFile -Tail $Lines
        if ((Test-Path $ErrFile) -and (Get-Item $ErrFile).Length -gt 0) {
            Write-Host ''
            Write-Host 'Ошибки (logsot.err.log):' -ForegroundColor Red
            Get-Content $ErrFile -Tail $Lines
        }
    }
}
