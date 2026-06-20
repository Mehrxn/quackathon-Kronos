# Loki Log Viewer - PowerShell
# Simple script to fetch and display logs from Loki

param(
    [string]$Container = "ai-agent-backend",
    [string]$Filter = "",
    [int]$Limit = 10,
    [int]$HoursBack = 2,
    [switch]$Live,
    [switch]$Errors,
    [switch]$Crashes,
    [switch]$All
)

$LOKI_URL = "http://localhost:3100"

function Write-Banner {
    Clear-Host
    Write-Host ""
    Write-Host "╔══════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "║                   LOKI LOG VIEWER                            ║" -ForegroundColor Cyan
    Write-Host "╚══════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""
}

function Get-Logs {
    param(
        [string]$Query,
        [int]$Limit = 10,
        [int]$HoursBack = 2
    )
    
    $endTime = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() * 1000000000)
    $startTime = $endTime - [int64]($HoursBack * 3600 * 1000000000)
    
    $encodedQuery = [System.Web.HttpUtility]::UrlEncode($Query)
    $url = "$LOKI_URL/loki/api/v1/query_range?query=$encodedQuery&limit=$Limit&direction=backward&start=$startTime&end=$endTime"
    
    try {
        $response = Invoke-RestMethod -Uri $url -TimeoutSec 10 -ErrorAction Stop
        return $response.data.result
    } catch {
        Write-Host "❌ Failed to connect to Loki: $($_.Exception.Message)" -ForegroundColor Red
        return @()
    }
}

function Format-LogEntry {
    param([string]$Line, [string]$Timestamp)
    
    # Try to parse as JSON first
    try {
        $parsed = $Line | ConvertFrom-Json
        $message = if ($parsed.message) { $parsed.message } else { $Line }
        $level = if ($parsed.level) { $parsed.level } else { "INFO" }
        $extra = if ($parsed.extra_fields) { $parsed.extra_fields } else { @{} }
        
        return @{
            Timestamp = $Timestamp
            Level = $level
            Message = $message
            Extra = $extra
            IsJson = $true
        }
    } catch {
        # Plain text log
        return @{
            Timestamp = $Timestamp
            Level = "INFO"
            Message = $Line
            Extra = @{}
            IsJson = $false
        }
    }
}

function Show-Logs {
    param([array]$Results, [string]$Title = "Logs")
    
    if ($Results.Count -eq 0) {
        Write-Host "📭 No logs found" -ForegroundColor DarkGray
        Write-Host "   Generate some traffic first:" -ForegroundColor DarkGray
        Write-Host "   curl -X POST http://localhost:8000/api/trigger/cpu" -ForegroundColor DarkGray
        return
    }
    
    Write-Host "📊 $Title ($($Results.Count) entries)" -ForegroundColor Cyan
    Write-Host ("─" * 80)
    
    $count = 0
    foreach ($stream in $Results) {
        $containerName = if ($stream.stream.container) { $stream.stream.container } else { "unknown" }
        
        foreach ($entry in $stream.values) {
            $nsTimestamp = [int64]$entry[0]
            $timestamp = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]($nsTimestamp / 1000000)).DateTime.ToString("HH:mm:ss")
            $line = $entry[1]
            
            $log = Format-LogEntry -Line $line -Timestamp $timestamp
            
            # Determine icon based on content
            $icon = "🔵"
            $msgUpper = $log.Message.ToUpper()
            
            if ($msgUpper -match "CRITICAL|FATAL|SEGFAULT|CRASH") {
                $icon = "🚨"
            } elseif ($msgUpper -match "ERROR|EXCEPTION|FAILED") {
                $icon = "🔴"
            } elseif ($msgUpper -match "WARNING|WARN|HIGH CPU|HIGH RAM|HIGH MEMORY") {
                $icon = "🟡"
            } elseif ($msgUpper -match "STARTED|COMPLETED|SUCCESS") {
                $icon = "🟢"
            }
            
            # Check extra fields for status
            if ($log.Extra.status -eq "crash") { $icon = "🚨" }
            if ($log.Extra.status -match "error") { $icon = "🔴" }
            if ($log.Extra.status -match "warning") { $icon = "🟡" }
            
            # Display log
            Write-Host "[$($log.Timestamp)] " -NoNewline -ForegroundColor DarkGray
            Write-Host "$icon " -NoNewline
            
            # Show message (truncate if too long)
            $displayMsg = $log.Message
            if ($displayMsg.Length -gt 120) {
                $displayMsg = $displayMsg.Substring(0, 117) + "..."
            }
            Write-Host $displayMsg
            
            # Show extra details for crashes/errors
            if ($log.Extra.Count -gt 0 -and ($icon -eq "🚨" -or $icon -eq "🔴")) {
                foreach ($key in $log.Extra.Keys) {
                    if ($key -ne "message" -and $key -ne "status") {
                        $val = $log.Extra[$key]
                        if ($val -is [string] -and $val.Length -gt 60) {
                            $val = $val.Substring(0, 57) + "..."
                        }
                        Write-Host "          ↳ $key`: $val" -ForegroundColor DarkGray
                    }
                }
            }
            
            $count++
        }
    }
    Write-Host ("─" * 80)
}

function Start-LiveTail {
    param([string]$Query)
    
    Write-Host "🔄 LIVE LOG STREAM - Press Ctrl+C to stop" -ForegroundColor Yellow
    Write-Host ("─" * 80)
    
    $lastTimestamp = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() * 1000000000)
    
    while ($true) {
        $endTime = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() * 1000000000)
        $encodedQuery = [System.Web.HttpUtility]::UrlEncode($Query)
        $url = "$LOKI_URL/loki/api/v1/query_range?query=$encodedQuery&limit=50&direction=forward&start=$lastTimestamp&end=$endTime"
        
        try {
            $response = Invoke-RestMethod -Uri $url -TimeoutSec 10 -ErrorAction Stop
            $results = $response.data.result
            
            foreach ($stream in $results) {
                foreach ($entry in $stream.values) {
                    $nsTimestamp = [int64]$entry[0]
                    $timestamp = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]($nsTimestamp / 1000000)).DateTime.ToString("HH:mm:ss")
                    $line = $entry[1]
                    
                    $log = Format-LogEntry -Line $line -Timestamp $timestamp
                    
                    # Icon
                    $icon = "🔵"
                    $msgUpper = $log.Message.ToUpper()
                    if ($msgUpper -match "CRITICAL|FATAL|SEGFAULT") { $icon = "🚨" }
                    elseif ($msgUpper -match "ERROR") { $icon = "🔴" }
                    elseif ($msgUpper -match "WARNING|HIGH") { $icon = "🟡" }
                    
                    $displayMsg = if ($log.Message.Length -gt 100) { $log.Message.Substring(0, 97) + "..." } else { $log.Message }
                    Write-Host "[$($log.Timestamp)] $icon $displayMsg"
                    
                    $lastTimestamp = [Math]::Max($lastTimestamp, $nsTimestamp + 1)
                }
            }
        } catch {
            # Silently retry
        }
        
        Start-Sleep -Seconds 2
    }
}

# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

Write-Banner

# Check Loki health
Write-Host "Checking Loki... " -NoNewline
try {
    $health = Invoke-RestMethod -Uri "$LOKI_URL/loki/api/v1/label" -TimeoutSec 5 -ErrorAction Stop
    Write-Host "✅ Connected" -ForegroundColor Green
} catch {
    Write-Host "❌ Cannot connect to Loki at $LOKI_URL" -ForegroundColor Red
    Write-Host "   Make sure Loki is running: docker-compose ps loki"
    exit 1
}

Write-Host ""

# Determine what to show based on flags
if ($Live) {
    # Live tail mode
    $query = "{container=`"$Container`"}"
    if ($Filter) {
        $query += " |= `"$Filter`""
    }
    if ($Errors) {
        $query = "{container=`"$Container`"} |= `"error|ERROR|Error`""
    }
    if ($Crashes) {
        $query = "{container=`"$Container`"} |= `"CRITICAL|FATAL|segfault|crash`""
    }
    Start-LiveTail -Query $query
    exit 0
}

# Show different log categories
if ($All -or (-not $Errors -and -not $Crashes)) {
    # Recent activity
    Write-Host "🔵 RECENT ACTIVITY (Last $HoursBack hours)" -ForegroundColor Blue
    $query = "{container=`"$Container`"}"
    if ($Filter) {
        $query += " |= `"$Filter`""
    }
    $results = Get-Logs -Query $query -Limit $Limit -HoursBack $HoursBack
    Show-Logs -Results $results -Title "Recent Logs"
    Write-Host ""
}

# Always show errors if not in specific mode
if ($Errors -or (-not $Crashes -and -not $All)) {
    Write-Host "🔴 ERRORS & WARNINGS" -ForegroundColor Red
    $query = "{container=`"$Container`"} |= `"error|ERROR|Error|warning|WARNING|Warn`""
    $results = Get-Logs -Query $query -Limit 5 -HoursBack $HoursBack
    Show-Logs -Results $results -Title "Errors & Warnings"
    Write-Host ""
}

# Crashes
if ($Crashes -or (-not $Errors -and -not $All)) {
    Write-Host "🚨 CRASHES & CRITICAL" -ForegroundColor Magenta
    $query = "{container=`"$Container`"} |= `"CRITICAL|FATAL|segfault|crash|SIGSEGV|panic`""
    $results = Get-Logs -Query $query -Limit 5 -HoursBack $HoursBack
    Show-Logs -Results $results -Title "Crashes & Critical Errors"
    Write-Host ""
}

# Summary
Write-Host "💡 Quick commands:" -ForegroundColor DarkGray
Write-Host "   .\view_logs.ps1 -Live              # Live tail logs"
Write-Host "   .\view_logs.ps1 -Errors            # Show only errors"
Write-Host "   .\view_logs.ps1 -Crashes           # Show only crashes"
Write-Host "   .\view_logs.ps1 -All -Limit 20     # Show 20 recent logs"
Write-Host "   .\view_logs.ps1 -Container ai-agent-frontend  # Frontend logs"
Write-Host ""
Write-Host "   Generate test alerts:" -ForegroundColor DarkGray
Write-Host "   curl -X POST http://localhost:8000/api/trigger/segfault"
Write-Host "   curl -X POST http://localhost:8000/api/trigger/cpu"
Write-Host ""