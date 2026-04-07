import requests
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

headers = {
    'X-API-Key': 'flowwatch-dev-key-001',
    'Content-Type': 'application/json'
}

hosts = ['host-01', 'host-02', 'host-03', 'host-04', 'host-05']

def normal(host):
    return {'host_id': host, 'latency_ms': abs(random.gauss(45, 10)),
            'packet_loss_pct': random.uniform(0, 2),
            'dns_failure_rate': random.uniform(0, 0.05),
            'jitter_ms': abs(random.gauss(8, 3))}

def spike(host):
    return {'host_id': host, 'latency_ms': random.uniform(300, 800),
            'packet_loss_pct': random.uniform(0, 2),
            'dns_failure_rate': random.uniform(0, 0.05),
            'jitter_ms': abs(random.gauss(8, 3))}

def loss(host):
    return {'host_id': host, 'latency_ms': abs(random.gauss(45, 10)),
            'packet_loss_pct': random.uniform(15, 40),
            'dns_failure_rate': random.uniform(0, 0.05),
            'jitter_ms': abs(random.gauss(8, 3))}

def dns_anomaly(host):
    return {'host_id': host, 'latency_ms': abs(random.gauss(45, 10)),
            'packet_loss_pct': random.uniform(0, 2),
            'dns_failure_rate': random.uniform(0.4, 0.9),
            'jitter_ms': abs(random.gauss(8, 3))}

def jitter_anomaly(host):
    return {'host_id': host, 'latency_ms': abs(random.gauss(45, 10)),
            'packet_loss_pct': random.uniform(0, 2),
            'dns_failure_rate': random.uniform(0, 0.05),
            'jitter_ms': random.uniform(80, 200)}

def congestion(host):
    return {'host_id': host, 'latency_ms': random.uniform(200, 500),
            'packet_loss_pct': random.uniform(8, 25),
            'dns_failure_rate': random.uniform(0, 0.05),
            'jitter_ms': abs(random.gauss(8, 3))}

def cascade(host):
    return {'host_id': host, 'latency_ms': random.uniform(400, 800),
            'packet_loss_pct': random.uniform(20, 40),
            'dns_failure_rate': random.uniform(0.5, 0.9),
            'jitter_ms': random.uniform(80, 150)}

anomaly_funcs = [spike, loss, dns_anomaly, jitter_anomaly, congestion, cascade]

def send(data):
    try:
        requests.post(
            'http://localhost:8000/telemetry/ingest',
            headers=headers,
            json=data,
            timeout=5
        )
    except:
        pass

# Phase 1 — build windows FAST using threads
print('Building windows fast (parallel)...')
records = []
for _ in range(32):
    for host in hosts:
        records.append(normal(host))

with ThreadPoolExecutor(max_workers=20) as ex:
    futures = [ex.submit(send, r) for r in records]
    done = 0
    for f in as_completed(futures):
        done += 1
        if done % 40 == 0:
            print(f'  {done}/{len(records)} sent')

print(f'Windows built! Sent {len(records)} records')
print()

# Phase 2 — chaos mode, all anomaly types, all hosts
print('Injecting random anomalies...')
records = []
for _ in range(100):
    for host in hosts:
        if random.random() < 0.4:
            data = random.choice(anomaly_funcs)(host)
        else:
            data = normal(host)
        records.append(data)

with ThreadPoolExecutor(max_workers=20) as ex:
    futures = [ex.submit(send, r) for r in records]
    done = 0
    for f in as_completed(futures):
        done += 1
        if done % 100 == 0:
            print(f'  {done}/{len(records)} sent')

print()
print('Done! Check your dashboard now!')