#!/bin/bash
# Generate test queries
queries=(
    "Analyze the quarterly sales data"
    "What are the top performing products?"
    "Show me customer satisfaction trends"
    "Predict next month's revenue"
    "Identify anomalies in the data"
    "Compare this year vs last year"
    "What's the conversion rate?"
    "Analyze user engagement metrics"
    "Show me the cohort analysis"
    "What are the key growth drivers?"
)

for i in {1..20}; do
    random_query=${queries[$RANDOM % ${#queries[@]}]}
    curl -X POST http://localhost:8000/api/agent/query \
      -H "Content-Type: application/json" \
      -d "{\"query\": \"$random_query\"}"
    sleep 0.5
    echo ""
done