# FlowWatch AI

Real-time network monitoring and anomaly detection system powered by ML and an LLM-based root cause analysis assistant.

**Why I Built This**

After finishing my previous project — MedSynth, a multi-agent RAG system for clinical summarization — I wanted to tackle something completely different. Something closer to systems and infrastructure rather than NLP. As suggested to me by Fred Weitendorf, Founder/CEO @Accretional on LinkedIn, I kept coming back to one question: how do large tech companies know their network is degrading before users start complaining? I wanted to understand how that actually works under the hood — not just read about it, but build it myself from scratch.

So I started FlowWatch AI. The goal was to build a real-time pipeline that ingests live network telemetry, detects anomalies using ML, and explains why something is wrong using an LLM assistant — all wired together end to end.

This is the most systems-heavy project I've worked on. I'm learning as I build.

**What It Does**

FlowWatch AI monitors network health across multiple hosts in real time. It tracks four key signals per host every second:

- Latency — how long packets take to travel
- Packet loss — what percentage of packets are being dropped
- DNS failure rate — how often DNS lookups are failing
- Jitter — how inconsistent the latency is

It runs two ML models on this data simultaneously, combines their outputs into a single anomaly score, and when something looks wrong it uses Claude (Anthropic's LLM) to explain the root cause in plain English.

## Live Dashboard

<img width="1166" height="899" alt="image" src="https://github.com/user-attachments/assets/73e38730-178d-41b2-b74a-802be7feaa3d" />

<img width="505" height="854" alt="image" src="https://github.com/user-attachments/assets/0f0e4202-0eb8-45c6-9ea3-8a17cd5dc11f" />

<img width="366" height="403" alt="image" src="https://github.com/user-attachments/assets/5b7797e0-78cf-4380-a7e2-895711651746" />




## Architecture

I designed this architecture by mapping out the data flow on paper first. The core idea was to keep each layer independent — the pipeline doesn't care about the models, the models don't care about the API, the API doesn't care about the frontend.

```
[Real Network Targets — live HTTP probes via real_producer.py]
  google.com  github.com  cloudflare.com  amazon.com  1.1.1.1
      |           |              |              |          |
      └───────────┴──────────────┴──────────────┴──────────┘
                                 |
                                 ▼
                ┌─────────────────────────┐
                │     AWS Kinesis Stream  │
                │   (LocalStack locally)  │
                │  partitioned by host_id │
                └────────────┬────────────┘
                             |
                             ▼
                ┌─────────────────────────┐
                │  Consumer + Preprocessor│  ← validates with Pydantic
                │                         │    normalizes to 0-1 range
                │                         │    computes health score
                └────────────┬────────────┘
                             |
                             ▼
                ┌─────────────────────────┐
                │   Feature Engineering   │  ← 19 features per host
                │   Sliding Window: 30s   │    statistical + temporal
                └──────────┬──────────────┘
                           |
                ┌──────────┴──────────┐
                ▼                     ▼
      ┌──────────────────┐  ┌──────────────────────┐
      │  LSTM Autoencoder│  │   Isolation Forest   │
      │   (PyTorch)      │  │   (Scikit-learn)     │
      │                  │  │                      │
      │  looks at last   │  │  looks at right now  │
      │  30s of history  │  │  is this a snapshot  │
      │  is this trending│  │  outlier?            │
      │  toward failure? │  │                      │
      └────────┬─────────┘  └──────────┬───────────┘
               │  score × 0.6          │ score × 0.4
               └──────────┬────────────┘
                          ▼
             ┌────────────────────────┐
             │   Anomaly Aggregator   │
             │ critical/high/med/low  │
             └────────────┬───────────┘
                          |
               ┌──────────┴──────────┐
               ▼                     ▼
      ┌─────────────────┐   ┌──────────────────┐
      │   FastAPI API   │   │  Alert Manager   │
      │  localhost:8000 │   │  + CloudWatch    │
      └────────┬────────┘   └──────────────────┘
               |
               ▼
      ┌──────────────────────────────┐
      │  Claude RCA Assistant        │  ← "why is github.com degrading?"
      │  (Anthropic API)             │    reads telemetry + anomaly context
      └────────┬─────────────────────┘
               |
               ▼
      ┌──────────────────────────────┐
      │   Next.js Dashboard          │  ← localhost:3000
      │   localhost:3000             │    Real-time charts, alert feed,
      └──────────────────────────────┘    AI assistant
```

---

### The ML Decision — Why These Two Models

This took me a while to figure out. My previous project used transformer-based architectures, and my first instinct here was to use an Autoencoder for anomaly detection since I'd worked with them before. But autoencoders for network anomaly detection have a real problem — they're good at reconstructing everything, including anomalies, once they've seen enough data. The reconstruction error stops being a reliable signal.

I went back to research and kept seeing Isolation Forest come up specifically for anomaly detection use cases. The intuition behind it is different from most ML models — instead of learning what normal looks like and measuring deviation, it asks "how hard is it to isolate this data point?" Anomalies are easy to isolate because they're already outliers. Normal points take many more splits to separate.

What I liked about combining it with LSTM:

- LSTM catches gradual degradation — latency slowly creeping up over 30 seconds, which is exactly the kind of thing that precedes a network failure
- Isolation Forest catches instant spikes — a single reading that's wildly outside normal range

Neither model catches everything on its own. Together they do.

The hardest part wasn't the architecture — it was tuning. Finding the right contamination rate for Isolation Forest, the right number of epochs for the LSTM before it starts overfitting, the right window size. I settled on 5% contamination, 50 epochs with early stopping, and a 30-second sliding window. These are not magic numbers — they're what worked on simulated data and will need retuning on real telemetry.

## Tech Stack

| Layer | Technology | Why |
|---|---|---|
| **Data Pipeline** | AWS Kinesis + boto3 | Industry standard for real-time streaming |
| **Local Dev** | LocalStack | Simulate AWS without cost |
| **ML — Temporal** | PyTorch LSTM Autoencoder | Best for sequential time-series patterns |
| **ML — Snapshot** | Scikit-learn Isolation Forest | Purpose-built for anomaly detection |
| **Feature Store** | NumPy + sliding window | 19 engineered features per host |
| **API** | FastAPI + Pydantic v2 | Async, fast, automatic validation |
| **LLM Assistant** | Anthropic Claude API | Root cause explanation in plain English |
| **Database** | TimescaleDB | PostgreSQL optimized for time-series |
| **Cache** | Redis | Fast feature vector lookup |
| **Frontend** | Next.js 14 + Recharts | Real-time dashboard |
| **Infra** | Docker Compose → AWS EC2 | Local first, cloud later |

## Feature Engineering

One thing I learned from MedSynth is that raw data rarely goes straight into a model. Here each raw telemetry record gets transformed into 19 features before hitting either model:

| Category | Features |
|---|---|
| **Statistical** | rolling mean + std of latency, loss, DNS, jitter over 30s window |
| **Trend** | linear regression slope of latency and health score |
| **Spike counts** | how many readings in the window exceeded danger thresholds |
| **Rate of change** | delta between last 2 readings for latency, loss, DNS |
| **Time encoding** | hour and weekday as sin/cos pairs (cyclic encoding) |
| **Composite** | health score: latency×0.4 + loss×0.3 + dns×0.2 + jitter×0.1 |

The time encoding was interesting — if you encode hour as a raw number (0–23), the model thinks midnight (0) and 11pm (23) are far apart. Cyclic sin/cos encoding fixes that.

### What I'm Still Learning

I want to be upfront about this:

AWS Kinesis and distributed streaming — I understand the concept (a managed stream that multiple producers write to and multiple consumers read from) but I'm still learning shard management and what happens when throughput limits are hit. LocalStack is helping me experiment locally before touching real AWS.

Production ML pipelines — there's a big gap between training a model in a notebook and serving it reliably at scale. I'm learning what that gap looks like by building through it.

The parts I understand well: the LSTM architecture, why autoencoders work for anomaly detection, the feature engineering decisions, and the overall system design.

## Project Structure

See [CLAUDE.md](CLAUDE.md) for the full project structure and architectural decisions.

## Build Phases

- [✅] Phase 1: Project scaffolding
- [✅] Phase 2: Telemetry simulator + Kinesis consumer
- [✅] Phase 3: Feature engineering pipeline
- [✅] Phase 4: Isolation Forest model
- [✅] Phase 5: LSTM model + training notebook
- [✅] Phase 6: FastAPI inference endpoints
- [✅] Phase 7: LLM RCA assistant
- [✅] Phase 8: Alert manager + CloudWatch integration
- [✅] Phase 9: Next.js real-time dashboard
- [✅] Phase 10: Docker Compose full-stack wiring
- [✅] Phase 11: Real website monitoring (google.com, github.com, cloudflare.com, amazon.com, 1.1.1.1)

## Results

### Dashboard Features (Live)
- Real-time latency charts for 5 hosts updating every 5 seconds
- Alert feed showing critical/high severity incidents only
- Per-metric indicators showing exactly which metric is anomalous
- AI-powered root cause analysis with 4-section structured diagnosis
- Host health status table sorted by severity
- 1,159+ telemetry records processed in testing

### ML Model Performance
- LSTM Autoencoder trained in 2.5 seconds on CPU
- Isolation Forest trained in 0.55 seconds
- Combined inference latency: ~40ms per prediction
- Anomaly detection confirmed on CASCADE events with combined score of 0.925/1.0
- Successfully distinguishes between:
  - Latency spikes (network congestion)
  - Packet loss (physical layer issues)
  - DNS failures (resolver/poisoning attacks)
  - Jitter anomalies (wireless interference)
  - CASCADE events (full outages/DDoS)

### AI Assistant Quality
- Gives specific diagnosis based on which metrics are anomalous
- References actual metric values in analysis
- Provides runnable terminal commands for investigation
- Different root cause for each anomaly type — not generic

## Running Locally

### Option 1 — Docker Compose (recommended, full stack)

```bash
# Start all services (TimescaleDB, Redis, LocalStack, backend, frontend)
docker-compose -f infra/docker-compose.yml up --build

# In a second terminal, init the Kinesis stream after LocalStack is ready
python scripts/init_kinesis.py

# Start real website monitoring
python scripts/real_producer.py --warmup
```

Then open:
- Dashboard: http://localhost:3000
- API docs: http://localhost:8000/docs

### Option 2 — Local (no Docker)

**Terminal 1 — Backend:**
```bash
cd flowwatch-ai-phase12
pip install -r backend/requirements.txt
uvicorn backend.api.main:app --reload
```

**Terminal 2 — Frontend:**
```bash
cd flowwatch-ai-phase12/frontend
npm install
npm run dev
```

**Terminal 3 — Send real network telemetry:**
```bash
# Warm up ML models first (sends 32 rounds of data)
python scripts/real_producer.py --warmup

# Then run continuous monitoring (default: one round every 10s)
python scripts/real_producer.py
```

Then open:
- Dashboard: http://localhost:3000
- API docs: http://localhost:8000/docs

## License

MIT
