// Simple frontend logger to replace Faro
class FrontendLogger {
    constructor() {
        this.logs = [];
    }
    
    logEvent(eventName, data) {
        const event = {
            timestamp: new Date().toISOString(),
            event: eventName,
            data: data,
            userAgent: navigator.userAgent
        };
        
        // Log to console for now
        console.log('[Frontend Event]', event);
        
        // Send to backend for logging
        this.sendToBackend(event);
    }
    
    async sendToBackend(event) {
        try {
            await fetch('http://localhost:8000/api/log', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(event)
            });
        } catch (error) {
            // Silently fail - don't affect user experience
        }
    }
    
    logError(error) {
        console.error('[Frontend Error]', error);
    }
}

const frontendLogger = new FrontendLogger();

class AIAgentClient {
    constructor() {
        this.baseUrl = 'http://localhost:8000';
        this.sessionId = null;
        this.queryCount = 0;
        this.totalResponseTime = 0;
        this.totalTokens = 0;
        
        this.initializeElements();
        this.setupEventListeners();
        this.checkBackendHealth();
    }
    
    initializeElements() {
        this.chatMessages = document.getElementById('chatMessages');
        this.queryInput = document.getElementById('queryInput');
        this.sendButton = document.getElementById('sendButton');
        this.newSessionBtn = document.getElementById('newSessionBtn');
        this.statusDot = document.getElementById('statusDot');
        this.statusText = document.getElementById('statusText');
        this.sessionIdElement = document.getElementById('sessionId');
        this.queryCountElement = document.getElementById('queryCount');
        this.avgResponseTimeElement = document.getElementById('avgResponseTime');
        this.tokensUsedElement = document.getElementById('tokensUsed');
    }
    
    setupEventListeners() {
        this.sendButton.addEventListener('click', () => this.sendQuery());
        this.queryInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendQuery();
            }
        });
        this.newSessionBtn.addEventListener('click', () => this.createNewSession());
    }
    
    async checkBackendHealth() {
        try {
            const response = await fetch(`${this.baseUrl}/health`);
            if (response.ok) {
                this.updateConnectionStatus(true);
                this.addMessage('system', 'Connected to AI agent backend successfully.');
            } else {
                this.updateConnectionStatus(false);
            }
        } catch (error) {
            this.updateConnectionStatus(false);
            this.addMessage('system', '⚠️ Unable to connect to backend. Please ensure the server is running.');
        }
    }
    
    updateConnectionStatus(connected) {
        if (connected) {
            this.statusDot.classList.add('connected');
            this.statusText.textContent = 'Connected';
        } else {
            this.statusDot.classList.remove('connected');
            this.statusText.textContent = 'Disconnected';
        }
    }
    
    async sendQuery() {
        const query = this.queryInput.value.trim();
        if (!query) return;
        
        // Track event using frontend logger
        frontendLogger.logEvent('query_sent', {
            queryLength: query.length,
            sessionId: this.sessionId
        });
        
        this.addMessage('user', query);
        this.queryInput.value = '';
        this.sendButton.disabled = true;
        
        const loadingMessage = this.addMessage('agent', '<div class="loading"></div> Thinking...');
        
        const startTime = performance.now();
        
        try {
            const response = await fetch(`${this.baseUrl}/api/agent/query`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    query: query,
                    session_id: this.sessionId
                })
            });
            
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            
            const data = await response.json();
            const responseTime = performance.now() - startTime;
            
            // Remove loading message
            loadingMessage.remove();
            
            // Display response
            const responseHtml = `
                ${data.response}
                <div class="message-meta">
                    <span>⏱️ ${data.thinking_time.toFixed(2)}s</span>
                    <span>🪙 ${data.tokens_used} tokens</span>
                    <span>🎯 ${(data.confidence * 100).toFixed(1)}% confidence</span>
                </div>
            `;
            this.addMessage('agent', responseHtml);
            
            // Update session
            if (!this.sessionId) {
                this.sessionId = data.session_id;
                this.sessionIdElement.textContent = this.sessionId.substring(0, 8) + '...';
            }
            
            this.queryCount++;
            this.totalResponseTime += responseTime;
            this.totalTokens += data.tokens_used;
            
            this.updateStats();
            
            // Track successful response using frontend logger
            frontendLogger.logEvent('query_completed', {
                responseTime,
                tokensUsed: data.tokens_used,
                confidence: data.confidence,
                sessionId: this.sessionId
            });
            
        } catch (error) {
            loadingMessage.remove();
            this.addMessage('system', `❌ Error: ${error.message}`);
            
            frontendLogger.logError(error);
        } finally {
            this.sendButton.disabled = false;
            this.queryInput.focus();
        }
    }
    
    addMessage(type, content) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${type}`;
        messageDiv.innerHTML = content;
        this.chatMessages.appendChild(messageDiv);
        this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
        return messageDiv;
    }
    
    updateStats() {
        this.queryCountElement.textContent = this.queryCount;
        this.avgResponseTimeElement.textContent = 
            this.queryCount > 0 ? `${(this.totalResponseTime / this.queryCount).toFixed(0)}ms` : '0ms';
        this.tokensUsedElement.textContent = this.totalTokens;
    }
    
    async createNewSession() {
        if (this.sessionId) {
            try {
                await fetch(`${this.baseUrl}/api/agent/sessions/${this.sessionId}`, {
                    method: 'DELETE'
                });
            } catch (error) {
                console.warn('Failed to close previous session:', error);
            }
        }
        
        this.sessionId = null;
        this.queryCount = 0;
        this.totalResponseTime = 0;
        this.totalTokens = 0;
        
        this.sessionIdElement.textContent = 'Not created';
        this.updateStats();
        this.chatMessages.innerHTML = '';
        this.addMessage('system', 'New session started. You can now send queries to the AI agent.');
        
        frontendLogger.logEvent('new_session_created', {});
    }
}

// Initialize the application
const app = new AIAgentClient();