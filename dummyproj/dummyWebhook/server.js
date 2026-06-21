const http = require('http');

const PORT = process.env.PORT || 8080;
// Inside the Docker network use 'http://loki:3100'; locally 'http://localhost:3100'.
const LOKI_BASE_URL = process.env.LOKI_URL || 'http://loki:3100';
// Kronos agent. NOTE: the correct path is /api/v1/init/ (the Grafana webhook
// target in kronos/api/app.py). The old /api/v1/incidents/webhook does not
// exist. If Kronos runs on the host while this runs in a container, reach it
// via host.docker.internal.
const AGENT_URL = process.env.AGENT_URL
    || 'http://host.docker.internal:8020/api/v1/init/';

/**
 * Determines the target LogQL query and filter expressions based on the Grafana Alert Type
 * @param {object} alert - The individual Grafana alert object
 */
function determineLogQLQuery(alert) {
    const alertName = (alert.labels?.alertname || '').toLowerCase();
    const description = (alert.annotations?.description || '').toLowerCase();
    const summary = (alert.annotations?.summary || '').toLowerCase();
    const appName = alert.labels?.app || alert.labels?.service || 'dummyBackend';

    // Base selector
    let baseSelector = `{app="${appName}"}`;
    if (alert.labels?.container) {
        baseSelector = `{container="${alert.labels.container}"}`;
    }

    // 1. RAM / Memory Alert
    if (alertName.includes('ram') || alertName.includes('memory') ||
        description.includes('ram') || description.includes('memory') ||
        summary.includes('ram') || summary.includes('memory')) {
        console.log(`🔍 [Loki Filter] Identified RAM/Memory alert. Applying resource filters.`);
        return `${baseSelector} |~ "(?i)(memory|oom|ram|alloc|out of memory|limit)"`;
    }

    // 2. vCPU / CPU Alert
    if (alertName.includes('cpu') || alertName.includes('vcpu') ||
        description.includes('cpu') || description.includes('vcpu') ||
        summary.includes('cpu') || summary.includes('vcpu')) {
        console.log(`🔍 [Loki Filter] Identified CPU/vCPU alert. Applying CPU load/limit filters.`);
        return `${baseSelector} |~ "(?i)(cpu|load|throttle|vcpu|limit|utilization)"`;
    }

    // 3. Crash / Segfault Alert
    if (alertName.includes('segfault') || description.includes('segfault') || summary.includes('segfault') ||
        alertName.includes('sigsegv') || description.includes('sigsegv') || summary.includes('sigsegv')) {
        console.log(`🔍 [Loki Filter] Identified Segfault crash alert. Applying crash/segfault filters.`);
        return `${baseSelector} |~ "(?i)(segfault|sigsegv|fatal|crash|dereference|panic)"`;
    }

    // 4. Out of Index Alert
    if (alertName.includes('outofindex') || alertName.includes('index') ||
        description.includes('outofindex') || description.includes('index') ||
        summary.includes('outofindex') || summary.includes('index')) {
        console.log(`🔍 [Loki Filter] Identified Out of Index alert. Applying index bounds filters.`);
        return `${baseSelector} |~ "(?i)(index|outofindex|out of index|range|bounds|overflow)"`;
    }

    // Fallback: Query for general errors, warnings, or failures if alert type is unrecognized
    console.log(`🔍 [Loki Filter] Unrecognized alert type. Falling back to general error filters.`);
    return `${baseSelector} |~ "(?i)(error|fail|exception|warn|critical)"`;
}

/**
 * Connects to Loki API and retrieves logs surrounding the alert time
 * @param {object} alert - The individual Grafana alert object
 * @param {object} originalPayload - The complete original webhook payload
 */
function queryLokiApi(alert, originalPayload) {
    const alertTimeISO = alert.startsAt;
    const alertName = alert.labels?.alertname || 'Unnamed Alert';

    const endMs = new Date(alertTimeISO).getTime();
    const startMs = endMs - (10 * 60 * 1000); // 10 minutes historical context window

    // Convert millisecond Unix times to absolute Nanoseconds for Loki
    const startNs = startMs * 1000000;
    const endNs = endMs * 1000000;

    // Determine target LogQL query based on alert type
    const logQL = determineLogQLQuery(alert);
    const limit = 50;

    const lokiEndpoint = `${LOKI_BASE_URL}/loki/api/v1/query_range?query=${encodeURIComponent(logQL)}&start=${startNs}&end=${endNs}&limit=${limit}&direction=BACKWARD`;

    console.log(`📡 [Webhook] Querying Loki API for "${alertName}": ${lokiEndpoint}`);

    http.get(lokiEndpoint, (res) => {
        let rawData = '';

        res.on('data', chunk => rawData += chunk);
        res.on('end', () => {
            try {
                const parsedData = JSON.parse(rawData);

                // Loki structural path: data -> result -> array of streams -> values
                const streams = parsedData.data?.result || [];
                let collectedLogLines = [];

                streams.forEach(stream => {
                    stream.values.forEach(logMatrix => {
                        // logMatrix[0] is the nanosecond timestamp, logMatrix[1] is the raw text log line
                        collectedLogLines.push(logMatrix[1]);
                    });
                });

                // Reverse lines since BACKWARD direction returns newest logs first
                collectedLogLines.reverse();

                console.log(`✅ [Loki Sync] Successfully retrieved ${collectedLogLines.length} contextual logs.`);

                // Send the specific firing alert + its fetched logs to Kronos.
                sendToAiEngine(alert, collectedLogLines);

            } catch (err) {
                console.error('❌ [Error] Failed parsing raw stream response from Loki:', err.message);
                sendToAiEngine(alert, []);
            }
        });
    }).on('error', (err) => {
        console.error('❌ [Connection Error] Webhook failed to hit Loki container API:', err.message);
        sendToAiEngine(alert, []);
    });
}

/**
 * Maps a Grafana alert into the Kronos InitRequest shape and forwards it.
 *
 * Kronos /api/v1/init/ expects (from kronos/api/app.py -> InitRequest):
 *   service          -> Incident.service
 *   instance         -> Incident.instance
 *   priority         -> Incident.declared_priority
 *   loki_logs        -> Incident.error_logs       (array of raw log line strings)
 *   prometheus_logs  -> Incident.prometheus_logs  (object; orchestrator reads .alertname)
 *
 * NOTE: field names/types are inferred from how app.py consumes them. If
 * schemas.py defines stricter types (e.g. priority as an enum, instance
 * required), adjust the mapping below to match and avoid 422 errors.
 *
 * @param {object} alert - The single firing Grafana alert object
 * @param {array} logs - The fetched logs array from Loki
 */
function sendToAiEngine(alert, logs) {
    console.log(`🤖 [AI Pipeline] Building Kronos InitRequest and sending to ${AGENT_URL}...`);

    const labels = alert.labels || {};
    const annotations = alert.annotations || {};

    const service = labels.app || labels.service || 'dummyBackend';
    const alertName = labels.alertname || 'generic';

    // Map Grafana priority. Adjust to whatever values Kronos's Priority enum
    // accepts (low/medium/high). Falls back to label, else null.
    const priority = labels.priority || null;

    const kronosPayload = {
        service: service,
        instance: labels.instance || labels.pod || labels.container || null,
        priority: priority,
        // Incident.error_logs expects the raw log line strings.
        loki_logs: logs,
        // Orchestrator does prometheus_logs.get("alertname"); keep that key,
        // and pass through the rest of the labels/annotations for context.
        prometheus_logs: {
            alertname: alertName,
            ...labels,
            ...annotations,
        },
    };

    const parsedUrl = new URL(AGENT_URL);
    const postData = JSON.stringify(kronosPayload);

    const options = {
        hostname: parsedUrl.hostname,
        port: parsedUrl.port || 80,
        path: parsedUrl.pathname + parsedUrl.search,
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(postData)
        }
    };

    const req = http.request(options, (res) => {
        let responseData = '';
        res.on('data', chunk => responseData += chunk);
        res.on('end', () => {
            console.log(`📡 [AI Pipeline] Agent response status: ${res.statusCode}`);
            console.log(`📡 [AI Pipeline] Agent response payload:`, responseData);
            if (res.statusCode === 422) {
                console.error('⚠️  [AI Pipeline] 422 from Kronos — InitRequest shape mismatch. '
                    + 'Check schemas.py field names/types against kronosPayload above.');
            }
        });
    });

    req.on('error', (err) => {
        console.error('❌ [AI Pipeline Error] Failed to send InitRequest to Kronos:', err.message);
    });

    req.write(postData);
    req.end();
}

// Webhook HTTP Router Server
const server = http.createServer((req, res) => {
    if (req.method === 'POST') {
        let body = '';
        req.on('data', chunk => body += chunk.toString());
        req.on('end', () => {
            try {
                const payload = JSON.parse(body);

                if (payload.alerts && payload.alerts.length > 0) {
                    payload.alerts.forEach(alert => {
                        if (alert.status === 'firing') {
                            const appName = alert.labels?.app || alert.labels?.service || 'dummyBackend';
                            const alertName = alert.labels?.alertname || 'Unnamed Alert';

                            console.log(`\n🚨 [Alert Received] Service "${appName}" triggered alert "${alertName}" (status: firing).`);
                            queryLokiApi(alert, payload);
                        }
                    });
                }

                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ status: 'fetching_context' }));
            } catch (e) {
                res.writeHead(400);
                res.end('Bad Alert Format');
            }
        });
    } else {
        res.writeHead(404);
        res.end();
    }
});

server.listen(PORT, () => console.log(`🚀 Webhook processor listening for Grafana Alerts on port ${PORT}`));