const http = require('http');

const PORT = process.env.PORT || 8080;
// Use 'http://localhost:3100' for local running, or 'http://loki:3100' if within docker-compose bridge network
const LOKI_BASE_URL = process.env.LOKI_URL || 'http://localhost:3100'; 
// Destination AI Agent Engine webhook URL
const AGENT_URL = process.env.AGENT_URL || 'http://localhost:8000/api/v1/incidents/webhook';

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
                
                // Send payload context down to Kronos AI Agent Engine
                sendToAiEngine(originalPayload, collectedLogLines);

            } catch (err) {
                console.error('❌ [Error] Failed parsing raw stream response from Loki:', err.message);
                sendToAiEngine(originalPayload, []);
            }
        });
    }).on('error', (err) => {
        console.error('❌ [Connection Error] Webhook failed to hit Loki container API:', err.message);
        sendToAiEngine(originalPayload, []);
    });
}

/**
 * Forwards compiled logs directly to your AI engine process node
 * @param {object} originalPayload - The original webhook payload from Grafana
 * @param {array} logs - The fetched logs array from Loki
 */
function sendToAiEngine(originalPayload, logs) {
    console.log(`🤖 [AI Pipeline] Preparing to send enriched webhook payload to Kronos Engine at ${AGENT_URL}...`);

    // Create a copy of the payload to avoid side-effects
    const enrichedPayload = { ...originalPayload };

    // Enrich the payload with the retrieved logs
    enrichedPayload.logs = logs;
    if (enrichedPayload.alerts && enrichedPayload.alerts.length > 0) {
        enrichedPayload.alerts = enrichedPayload.alerts.map(alert => ({
            ...alert,
            logs: logs
        }));
    }

    const parsedUrl = new URL(AGENT_URL);
    const postData = JSON.stringify(enrichedPayload);

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
        });
    });

    req.on('error', (err) => {
        console.error('❌ [AI Pipeline Error] Failed to send enriched payload to Kronos Agent:', err.message);
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
