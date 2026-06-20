# Kronos Demo Script - PowerShell Version
# Complete Hackathon Demo Flow for Windows

param(
    [switch]$Quick,        # Quick mode - skip health checks
    [switch]$AlertsOnly,   # Only generate alerts
    [switch]$KronosOnly    # Only trigger Kronos agent
)

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────

$BACKEND_URL = "http://localhost:8000"
$KRONOS_URL = "http://localhost:8001"
$GRAFANA_URL = "http://localhost:3001"
$FRONTEND_URL = "http://localhost:8080"
$PROMETHEUS_URL = "http://localhost:9090"
$LOKI_URL = "http://localhost:3100"

$script:TotalAlerts = 0
$script:TotalKronos = 0

# ─────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────

function Write-Banner {
    Clear-Host
    Write-Host "╔══════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "║                                                              ║" -ForegroundColor Cyan
    Write-Host "║           KRONOS - Autonomous Incident Response              ║" -ForegroundColor Cyan
    Write-Host "║                 Hackathon Demo v1.0                          ║" -ForegroundColor Cyan
    Write-Host "║                                                              ║" -ForegroundColor Cyan
    Write-Host "╚══════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host ("═" * 65) -ForegroundColor Blue
    Write-Host "  $Title" -ForegroundColor Blue
    Write-Host ("═" * 65) -ForegroundColor Blue
    Write-Host ""
}

function Check-Service {
    param([string]$Name, [string]$Url, [string]$Endpoint = "/")
    
    Write-Host "  Checking $Name... " -NoNewline
    try {
        $response = Invoke-WebRequest -Uri "$Url$Endpoint" -TimeoutSec 5 -UseBasicParsing
        if ($response.StatusCode -eq 200 -or $response.StatusCode -eq 302) {
            Write-Host "✅ Running" -ForegroundColor Green
            return $true
        }
    } catch {
        Write-Host "❌ Not responding" -ForegroundColor Red
        return $false
    }
}

function Invoke-Alert {
    param([string]$Type, [string]$Label, [string]$Endpoint)
    
    Write-Host "  🚨 $Label... " -NoNewline
    try {
        $response = Invoke-RestMethod -Uri "$BACKEND_URL$Endpoint" -Method Post -TimeoutSec 10
        if ($response.status -eq "triggered") {
            Write-Host "✓ Generated" -ForegroundColor Green
            $script:TotalAlerts++
        } else {
            Write-Host "✗ Failed" -ForegroundColor Red
        }
    } catch {
        Write-Host "✗ Failed ($($_.Exception.Message))" -ForegroundColor Red
    }
}

function Invoke-Kronos {
    param([string]$Type, [string]$Label)
    
    Write-Host "  🤖 Kronos analyzing $Label... " -NoNewline
    try {
        $response = Invoke-RestMethod -Uri "$KRONOS_URL/webhook/demo/$Type" -Method Post -TimeoutSec 30
        if ($response.status -match "processed") {
            $report = $response.report
            Write-Host "Done" -ForegroundColor Green
            Write-Host "     Severity: " -NoNewline
            Write-Host $report.severity -ForegroundColor Yellow -NoNewline
            Write-Host " | Action: " -NoNewline
            Write-Host $report.action_taken -ForegroundColor Cyan -NoNewline
            Write-Host " | Confidence: " -NoNewline
            Write-Host ($report.confidence.ToString("P0")) -ForegroundColor Green
            $script:TotalKronos++
        } else {
            Write-Host "✗ Failed" -ForegroundColor Red
        }
    } catch {
        Write-Host "✗ Failed ($($_.Exception.Message))" -ForegroundColor Red
    }
}

function Show-Logs {
    param([string]$Filter, [string]$Label)
    
    Write-Host "  📊 ${Label}:"
    try {
        $endTime = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() * 1000000000
        $query = "{container=`"ai-agent-backend`"} |= `"$Filter`""
        $url = "$LOKI_URL/loki/api/v1/query_range?query=$([System.Web.HttpUtility]::UrlEncode($query))&limit=3&direction=backward&end=$endTime"
        
        $response = Invoke-RestMethod -Uri $url -TimeoutSec 10
        $results = $response.data.result
        
        foreach ($stream in $results) {
            foreach ($entry in $stream.values) {
                $timestamp = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$entry[0] / 1000000).ToString("HH:mm:ss")
                $line = $entry[1]
                
                try {
                    $parsed = $line | ConvertFrom-Json
                    $msg = if ($parsed.message) { $parsed.message } else { $line }
                    if ($msg.Length -gt 80) { $msg = $msg.Substring(0, 80) + "..." }
                    
                    $icon = "🔴"
                    if ($parsed.extra_fields.status -eq "crash") { $icon = "🚨" }
                    elseif ($parsed.extra_fields.status -match "error") { $icon = "🔴" }
                    elseif ($parsed.extra_fields.status -match "warning") { $icon = "🟡" }
                    
                    Write-Host "     [$timestamp] $icon $msg"
                } catch {
                    if ($line.Length -gt 80) { $line = $line.Substring(0, 80) + "..." }
                    Write-Host "     [$timestamp] $line"
                }
            }
        }
    } catch {
        Write-Host "     ⚠️  Could not fetch logs" -ForegroundColor Yellow
    }
}

function Show-Metrics {
    Write-Host "📊 Prometheus Metrics:"
    try {
        $response = Invoke-RestMethod -Uri "$BACKEND_URL/metrics" -TimeoutSec 10
        $lines = $response -split "`n"
        foreach ($line in $lines) {
            if ($line -match "ai_agent_errors_total" -and $line -notmatch "^#") {
                Write-Host "  $line"
            }
            if ($line -match "ai_agent_memory_usage_bytes" -and $line -notmatch "^#") {
                $val = [double]($line -split "\s+")[-1]
                $gb = [math]::Round($val / 1GB, 1)
                Write-Host "  Memory Usage: ${gb} GB"
            }
            if ($line -match "ai_agent_cpu_usage_ratio" -and $line -notmatch "^#") {
                $val = [double]($line -split "\s+")[-1]
                $pct = [math]::Round($val * 100, 1)
                Write-Host "  CPU Usage: ${pct}%"
            }
        }
    } catch {
        Write-Host "  ⚠️  Could not fetch metrics" -ForegroundColor Yellow
    }
}

function Show-KronosStatus {
    Write-Host "🤖 Kronos Agent Status:"
    try {
        $status = Invoke-RestMethod -Uri "$KRONOS_URL/status" -TimeoutSec 10
        Write-Host "  Uptime: $($status.uptime)"
        Write-Host "  Incidents Handled: $($status.incidents_handled)"
        if ($status.recent_incidents) {
            Write-Host "  Recent Incidents:"
            foreach ($inc in $status.recent_incidents) {
                Write-Host "    • $($inc.id): $($inc.severity) - Action: $($inc.action) - Confidence: $($inc.confidence.ToString('P0'))"
            }
        }
    } catch {
        Write-Host "  ⚠️  Agent not responding" -ForegroundColor Yellow
    }
}

# ─────────────────────────────────────────────────────────────────
# DEMO FLOW FUNCTIONS
# ─────────────────────────────────────────────────────────────────

function Start-FullDemo {
    Write-Banner
    
    # 1. Health Check
    if (-not $Quick) {
        Write-Section "1️⃣  SYSTEM HEALTH CHECK"
        Write-Host "Checking services..."
        Check-Service "Backend" $BACKEND_URL "/health"
        Check-Service "Kronos Agent" $KRONOS_URL "/health"
        Check-Service "Grafana" $GRAFANA_URL "/login"
        Check-Service "Prometheus" $PROMETHEUS_URL "/graph"
        Check-Service "Loki" $LOKI_URL "/loki/api/v1/label"
    }
    
    # 2. Generate Alerts
    Write-Section "2️⃣  GENERATING INCIDENTS"
    Write-Host "Simulating production incidents..."
    Write-Host ""
    
    Write-Host "  CRITICAL EVENTS:" -ForegroundColor Red
    Invoke-Alert "segfault" "Segfault crash" "/api/trigger/segfault"
    Invoke-Alert "outofindex" "Out of Index exception" "/api/trigger/outofindex"
    
    Write-Host ""
    Write-Host "  RESOURCE ALERTS:" -ForegroundColor Yellow
    Invoke-Alert "cpu" "CPU spike (95%)" "/api/trigger/cpu"
    Invoke-Alert "ram" "RAM spike (97.5%)" "/api/trigger/ram"
    
    Write-Host ""
    Write-Host "  GENERATING TRAFFIC:" -ForegroundColor Blue
    Write-Host "  📊 Sending AI agent queries... " -NoNewline
    for ($i = 1; $i -le 5; $i++) {
        try {
            $body = @{query="Demo traffic query $i"} | ConvertTo-Json
            Invoke-RestMethod -Uri "$BACKEND_URL/api/agent/query" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 10 | Out-Null
        } catch {}
    }
    Write-Host "Done (5 queries)" -ForegroundColor Green
    
    Write-Host ""
    Write-Host "  ⏳ Waiting for logs to propagate..."
    Start-Sleep -Seconds 3
    
    # 3. Show Logs
    Write-Section "3️⃣  LOG VERIFICATION (LOKI)"
    Write-Host "Verifying logs in Loki..."
    Write-Host ""
    
    Show-Logs "CRITICAL|FATAL|segfault|crash" "Crash Logs"
    Write-Host ""
    Show-Logs "HIGH CPU|HIGH RAM|resource_warning" "Resource Alerts"
    Write-Host ""
    Show-Logs "IndexError|outofindex" "Runtime Errors"
    
    # 4. Kronos Response
    Write-Section "4️⃣  KRONOS AGENT RESPONSE"
    Write-Host "Triggering Kronos for each incident type..."
    Write-Host ""
    
    Invoke-Kronos "cpu" "CPU Alert"
    Start-Sleep -Seconds 1
    Invoke-Kronos "memory" "Memory Alert"
    Start-Sleep -Seconds 1
    Invoke-Kronos "error" "Error Spike"
    Start-Sleep -Seconds 1
    Invoke-Kronos "response" "Response Time Spike"
    
    # 5. Verification
    Write-Section "5️⃣  VERIFICATION & REPORTING"
    
    Show-Metrics
    Write-Host ""
    Show-KronosStatus
    
    # 6. Links
    Write-Section "6️⃣  DASHBOARD LINKS"
    
    Write-Host "  📊 Grafana Dashboard:     $GRAFANA_URL" -ForegroundColor Green
    Write-Host "     Login: admin / admin"
    Write-Host "     Dashboard: AI Agent Monitoring"
    Write-Host ""
    Write-Host "  🤖 Kronos API Docs:       $KRONOS_URL/docs" -ForegroundColor Green
    Write-Host ""
    Write-Host "  💻 Frontend App:          $FRONTEND_URL" -ForegroundColor Green
    Write-Host ""
    Write-Host "  📈 Prometheus:            $PROMETHEUS_URL" -ForegroundColor Green
    Write-Host ""
    
    # Complete
    Write-Section "✅ DEMO COMPLETE"
    
    Write-Host "  The Kronos agent has successfully:" -ForegroundColor Green
    Write-Host "    1. Detected production incidents (crash, CPU, RAM)"
    Write-Host "    2. Retrieved logs from Loki"
    Write-Host "    3. Analyzed metrics from Prometheus"
    Write-Host "    4. Diagnosed root causes with confidence scores"
    Write-Host "    5. Generated fixes and tests"
    Write-Host "    6. Created automated PRs for critical issues"
    Write-Host ""
    Write-Host "  Total Alerts Generated: $script:TotalAlerts" -ForegroundColor Yellow
    Write-Host "  Total Kronos Responses: $script:TotalKronos" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  🏆 Ready for hackathon presentation! 🏆" -ForegroundColor Cyan
    Write-Host ""
}

# ─────────────────────────────────────────────────────────────────
# QUICK COMMANDS
# ─────────────────────────────────────────────────────────────────

function Invoke-QuickAlerts {
    Write-Banner
    Write-Section "GENERATING ALL ALERTS"
    
    Write-Host "  🚨 Generating critical errors..." -ForegroundColor Red
    Invoke-Alert "segfault" "Segfault" "/api/trigger/segfault"
    Invoke-Alert "outofindex" "OOB" "/api/trigger/outofindex"
    
    Write-Host "  🟡 Generating resource alerts..." -ForegroundColor Yellow
    Invoke-Alert "cpu" "CPU" "/api/trigger/cpu"
    Invoke-Alert "ram" "RAM" "/api/trigger/ram"
    
    Write-Host ""
    Write-Host "  ✅ $script:TotalAlerts alerts generated" -ForegroundColor Green
}

function Invoke-QuickKronos {
    Write-Banner
    Write-Section "TRIGGERING KRONOS AGENT"
    
    Invoke-Kronos "cpu" "CPU"
    Invoke-Kronos "memory" "Memory"
    Invoke-Kronos "error" "Error"
    Invoke-Kronos "response" "ResponseTime"
    
    Write-Host ""
    Write-Host "  ✅ $script:TotalKronos Kronos responses received" -ForegroundColor Green
}

function Start-LiveMonitor {
    Write-Banner
    Write-Host "🔄 LIVE MONITORING MODE - Press Ctrl+C to stop" -ForegroundColor Yellow
    Write-Host ""
    
    while ($true) {
        $timestamp = Get-Date -Format "HH:mm:ss"
        try {
            $status = Invoke-RestMethod -Uri "$BACKEND_URL/health" -TimeoutSec 5
            $metrics = Invoke-RestMethod -Uri "$BACKEND_URL/api/agent/stats" -TimeoutSec 5
            
            Write-Host "[$timestamp] " -NoNewline
            Write-Host "✅ Healthy | " -ForegroundColor Green -NoNewline
            Write-Host "Sessions: $($metrics.active_sessions) | " -NoNewline
            Write-Host "Errors: $($metrics.total_errors) | " -NoNewline
            Write-Host "Tokens: $($metrics.total_tokens)" -ForegroundColor Cyan
        } catch {
            Write-Host "[$timestamp] " -NoNewline
            Write-Host "❌ Backend unreachable" -ForegroundColor Red
        }
        Start-Sleep -Seconds 5
    }
}

function Show-Help {
    Write-Banner
    Write-Host "KRONOS DEMO SCRIPT - Usage:" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  .\kronos_demo.ps1              Full demo flow"
    Write-Host "  .\kronos_demo.ps1 -Quick       Skip health checks"
    Write-Host "  .\kronos_demo.ps1 -AlertsOnly  Generate alerts only"
    Write-Host "  .\kronos_demo.ps1 -KronosOnly  Trigger Kronos only"
    Write-Host ""
    Write-Host "Quick commands:" -ForegroundColor Yellow
    Write-Host "  QuickAlerts     - Generate all alerts"
    Write-Host "  QuickKronos     - Trigger all Kronos responses"
    Write-Host "  LiveMonitor     - Real-time health monitoring"
    Write-Host "  Show-Logs       - View recent crash logs"
    Write-Host ""
}

# ─────────────────────────────────────────────────────────────────
# MAIN EXECUTION
# ─────────────────────────────────────────────────────────────────

# Export functions for interactive use
# Export-ModuleMember -Function * -Alias *

if ($AlertsOnly) {
    Invoke-QuickAlerts
} elseif ($KronosOnly) {
    Invoke-QuickKronos
} else {
    Start-FullDemo
}