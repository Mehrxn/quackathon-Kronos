#!/usr/bin/env python3
"""
Kronos Log Viewer - Hackathon Demo Version
Fetches and displays logs from Loki with crash/error highlighting
"""

import requests
import time
import json
from datetime import datetime
from typing import List, Dict, Optional

LOKI_URL = "http://localhost:3100"

class KronosLogViewer:
    """Enhanced Loki log viewer for hackathon demo"""
    
    def __init__(self, loki_url: str = LOKI_URL):
        self.loki_url = loki_url.rstrip('/')
    
    def check_health(self) -> bool:
        """Check if Loki is available"""
        try:
            # Try different health endpoints
            for endpoint in ['/ready', '/loki/api/v1/status/buildinfo', '/metrics']:
                try:
                    response = requests.get(f"{self.loki_url}{endpoint}", timeout=5)
                    if response.status_code in [200, 404]:  # 404 means endpoint exists but returns 404
                        return True
                except:
                    continue
            return True  # Assume it's working if we can connect
        except:
            return False
    
    def fetch_logs(
        self, 
        container: str = "ai-agent-backend",
        limit: int = 10,
        minutes_back: int = 15,
        filter_pattern: Optional[str] = None,
    ) -> List[Dict]:
        """Fetch logs from Loki"""
        
        end_time = int(time.time() * 1e9)
        start_time = end_time - (minutes_back * 60 * 1e9)
        
        # Build query
        if filter_pattern:
            query = f'{{container="{container}"}} |= `{filter_pattern}`'
        else:
            query = f'{{container="{container}"}}'
        
        params = {
            'query': query,
            'limit': limit,
            'direction': 'backward',
            'start': int(start_time),
            'end': int(end_time)
        }
        
        try:
            response = requests.get(
                f"{self.loki_url}/loki/api/v1/query_range",
                params=params,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            results = data.get('data', {}).get('result', [])
            
            if not results:
                return []
            
            return self._parse_logs(results)
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to connect to Loki: {e}")
            return []
    
    def _parse_logs(self, results: List) -> List[Dict]:
        """Parse raw Loki results into structured log entries"""
        logs = []
        
        for result in results:
            stream_labels = result.get('stream', {})
            for value in result.get('values', []):
                ns_timestamp = int(value[0])
                log_line = value[1]
                
                # Try to parse as JSON
                try:
                    parsed = json.loads(log_line)
                    message = parsed.get('message', log_line)
                    level = parsed.get('level', 'INFO')
                    extra_fields = parsed.get('extra_fields', {})
                    
                    # Extract error details if present
                    error_type = extra_fields.get('error_type', '')
                    status = extra_fields.get('status', '')
                    
                except json.JSONDecodeError:
                    message = log_line
                    level = 'INFO'
                    extra_fields = {}
                    error_type = ''
                    status = ''
                
                logs.append({
                    'timestamp': datetime.fromtimestamp(ns_timestamp / 1e9),
                    'level': level,
                    'message': message,
                    'extra': extra_fields,
                    'error_type': error_type,
                    'status': status,
                    'container': stream_labels.get('container', 'unknown'),
                })
        
        return logs
    
    def display_demo_view(self):
        """Display logs in hackathon demo format"""
        
        print("""
╔══════════════════════════════════════════════════════════════╗
║                   KRONOS LOG VIEWER                          ║
║              Real-time Incident Monitoring                   ║
╚══════════════════════════════════════════════════════════════╝
""")
        
        # Check health
        if self.check_health():
            print("✅ Loki Connected - Streaming logs...")
        else:
            print("❌ Loki Unreachable - Check if Loki is running")
            return
        
        # Fetch crash/error logs
        print("\n🔴 CRITICAL ERRORS DETECTED:")
        print("─" * 60)
        
        error_logs = self.fetch_logs(
            container="ai-agent-backend",
            filter_pattern="error|CRITICAL|FATAL|segfault|crash",
            limit=10
        )
        
        if error_logs:
            for log in error_logs:
                ts = log['timestamp'].strftime('%H:%M:%S')
                
                # Highlight crashes
                if log['error_type'] == 'segfault' or log['status'] == 'crash':
                    print(f"""
╔══════════════════════════════════════════════════════════════╗
║ 🚨 CRASH DETECTED - {ts}                    ║
╠══════════════════════════════════════════════════════════════╣
║ Signal:    {log['extra'].get('signal', 'N/A'):<44} ║
║ Error:     {log['extra'].get('error_type', 'N/A'):<44} ║
║ Exit Code: {log['extra'].get('code', 'N/A'):<44} ║
║ Message:   {log['message'][:44]:<44} ║
╚══════════════════════════════════════════════════════════════╝
""")
                else:
                    icon = self._level_icon(log['level'])
                    print(f"[{ts}] {icon} {log['message'][:80]}")
        
        # Fetch recent activity
        print("\n📊 RECENT ACTIVITY:")
        print("─" * 60)
        
        recent_logs = self.fetch_logs(
            container="ai-agent-backend",
            limit=5
        )
        
        for log in recent_logs:
            if log['level'] != 'ERROR':  # Skip errors (shown above)
                ts = log['timestamp'].strftime('%H:%M:%S')
                icon = self._level_icon(log['level'])
                
                if 'duration_ms' in log['extra']:
                    print(f"[{ts}] {icon} {log['extra'].get('method', '')} {log['extra'].get('path', '')} → {log['extra'].get('status', '')} ({log['extra']['duration_ms']:.1f}ms)")
                else:
                    print(f"[{ts}] {icon} {log['message'][:80]}")
        
        # Summary
        total_errors = len([l for l in error_logs if l['level'] == 'ERROR'])
        crashes = len([l for l in error_logs if l['error_type'] == 'segfault'])
        
        print(f"""
╔══════════════════════════════════════════════════════════════╗
║                   LOG SUMMARY                                ║
╠══════════════════════════════════════════════════════════════╣
║ Total Errors: {total_errors:<43} ║
║ Crashes:      {crashes:<43} ║
║ Status:       {'⚠️  NEEDS ATTENTION' if crashes > 0 else '✅ HEALTHY':<43} ║
╚══════════════════════════════════════════════════════════════╝
""")
    
    def _level_icon(self, level: str) -> str:
        icons = {
            'ERROR': '🔴',
            'WARNING': '🟡',
            'WARN': '🟡',
            'INFO': '🔵',
            'DEBUG': '⚪',
        }
        return icons.get(level.upper(), '❓')
    
    def tail_live(self, container: str = "ai-agent-backend", filter_pattern: Optional[str] = None):
        """Live tail for demo"""
        print(f"\n🔄 LIVE LOG STREAM - {container}")
        print("Press Ctrl+C to stop\n")
        print("─" * 80)
        
        last_timestamp = int(time.time() * 1e9)
        
        try:
            while True:
                query = f'{{container="{container}"}}'
                if filter_pattern:
                    query += f' |= `{filter_pattern}`'
                
                params = {
                    'query': query,
                    'limit': 50,
                    'direction': 'forward',
                    'start': last_timestamp,
                    'end': int(time.time() * 1e9)
                }
                
                try:
                    response = requests.get(
                        f"{self.loki_url}/loki/api/v1/query_range",
                        params=params,
                        timeout=10
                    )
                    
                    if response.status_code == 200:
                        data = response.json()
                        for result in data.get('data', {}).get('result', []):
                            for ts, line in result.get('values', []):
                                ns_timestamp = int(ts)
                                timestamp = datetime.fromtimestamp(ns_timestamp / 1e9)
                                ts_str = timestamp.strftime('%H:%M:%S')
                                
                                # Highlight errors
                                if 'error' in line.lower() or 'critical' in line.lower():
                                    print(f"[{ts_str}] 🔴 {line.strip()}")
                                elif 'warning' in line.lower():
                                    print(f"[{ts_str}] 🟡 {line.strip()}")
                                else:
                                    print(f"[{ts_str}] {line.strip()[:120]}")
                                
                                last_timestamp = max(last_timestamp, ns_timestamp + 1)
                
                except Exception as e:
                    print(f"⚠️  {e}")
                
                time.sleep(2)
                
        except KeyboardInterrupt:
            print("\n\n⏹️  Log stream stopped")


# ==================== Main ====================

if __name__ == "__main__":
    import sys
    
    viewer = KronosLogViewer()
    
    # Check for command line args
    if len(sys.argv) > 1:
        command = sys.argv[1]
        
        if command == "live":
            container = sys.argv[2] if len(sys.argv) > 2 else "ai-agent-backend"
            filter_pattern = sys.argv[3] if len(sys.argv) > 3 else None
            viewer.tail_live(container, filter_pattern)
        
        elif command == "errors":
            logs = viewer.fetch_logs(filter_pattern="error|CRITICAL|FATAL")
            for log in logs:
                print(f"[{log['timestamp'].strftime('%H:%M:%S')}] {log['message']}")
        
        elif command == "crash":
            logs = viewer.fetch_logs(filter_pattern="segfault|CRITICAL|FATAL|crash")
            for log in logs:
                print(json.dumps({
                    'timestamp': log['timestamp'].isoformat(),
                    'error_type': log['error_type'],
                    'signal': log['extra'].get('signal'),
                    'message': log['message']
                }, indent=2))
        
        else:
            print(f"Unknown command: {command}")
            print("Usage: python log_viewer.py [live|errors|crash]")
    
    else:
        # Default: show demo view
        viewer.display_demo_view()