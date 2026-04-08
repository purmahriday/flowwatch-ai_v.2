"""
FlowWatch AI — Model Retraining on Real-World Data (Phase 12)
=============================================================
Retrains both the Isolation Forest and LSTM models using a mix of:

  1. Real telemetry pulled live from the running FlowWatch API.
  2. Synthetic data generated with real-world internet latency baselines
     (~150–290 ms) instead of the original simulated baseline (~45 ms).

Why:
  The original models were trained on synthetic data where "normal" latency
  was ~45 ms (simulated LAN). Real internet targets (google.com, github.com,
  cloudflare.com) produce 150–290 ms latency, which the old models classify
  as anomalous. This script retrains so that real-world latency is the new
  normal baseline.

Usage:
  # From project root, with the FlowWatch stack running:
  python scripts/retrain_models.py

  # Skip API fetch (use only synthetic data — useful if API is down):
  python scripts/retrain_models.py --synthetic-only

  # Custom number of synthetic samples:
  python scripts/retrain_models.py --n-synthetic 8000

After running, the updated .pt and .joblib artifacts are saved to
backend/models/artifacts/. Restart the backend container to pick them up:
  docker-compose -f infra/docker-compose.yml restart backend
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

# ── Path setup: run from project root ────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.models.feature_engineering import (
    FeatureExtractor,
    FeatureVector,
    WINDOW_SIZE,
    _IF_FEATURE_ORDER,
)
from backend.models.isolation_forest import IsolationForestDetector
from backend.models.lstm_model import LSTMAutoencoder, LSTMTrainer
from backend.pipeline.preprocessor import ProcessedRecord, preprocess
from backend.pipeline.kinesis_consumer import TelemetryRecord

# ── Constants ──────────────────────────────────────────────────────────────────

API_BASE = "http://localhost:8000"
API_KEY = "flowwatch-dev-key-001"

# Real-world baselines observed via Phase 12 real_producer.py
# Normalization bounds from preprocessor.py:
#   latency: [0, 1000] ms  → 150ms = 0.15, 290ms = 0.29
#   jitter:  [0, 200]  ms  → 15ms  = 0.075, 70ms = 0.35
#   loss:    [0, 100]  %   → 0%    = 0.0
#   dns:     [0, 1]        → 0.001–0.04

# "Healthy" sites: google.com, github.com, cloudflare.com
NORMAL_LAT_MEAN = 0.21   # ~210 ms on [0, 1000] scale
NORMAL_LAT_STD  = 0.05   # spread across healthy sites

NORMAL_JITTER_MEAN = 0.12  # ~24 ms on [0, 200] scale
NORMAL_JITTER_STD  = 0.06

NORMAL_LOSS_MEAN = 0.003   # ~0.3% average loss (nearly zero)
NORMAL_DNS_MEAN  = 0.018   # small background noise

# Anomaly overrides (same relative magnitude as original, just higher absolute)
SPIKE_LAT_MIN, SPIKE_LAT_MAX     = 0.65, 0.95   # 650–950 ms
LOSS_LOSS_MIN, LOSS_LOSS_MAX     = 0.25, 0.45
DNS_DNS_MIN,   DNS_DNS_MAX       = 0.55, 0.90

DUMMY_WINDOW = np.zeros((WINDOW_SIZE, 5), dtype=np.float64)


# ── Fetch real telemetry from API ─────────────────────────────────────────────

def fetch_real_records() -> list[ProcessedRecord]:
    """Pull recent telemetry from the live API and return ProcessedRecord list."""
    try:
        import requests
        url = f"{API_BASE}/telemetry/recent"
        params = {"minutes": 60, "limit": 1000}
        headers = {"X-API-Key": API_KEY}
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        records_raw = data.get("records", [])
        print(f"  Fetched {len(records_raw)} real records from API.")

        processed: list[ProcessedRecord] = []
        for r in records_raw:
            try:
                processed.append(ProcessedRecord(**r))
            except Exception:
                continue
        return processed
    except Exception as exc:
        print(f"  [WARN] Could not fetch from API: {exc}")
        return []


# ── Real-data FeatureVector builder ───────────────────────────────────────────

def build_feature_vectors_from_records(
    records: list[ProcessedRecord],
) -> list[FeatureVector]:
    """
    Feed ProcessedRecords through a fresh FeatureExtractor (in timestamp order)
    to produce FeatureVectors.  Each host gets its own sliding-window buffer
    inside the extractor.
    """
    extractor = FeatureExtractor()
    # Sort oldest first so the window fills in order
    sorted_records = sorted(records, key=lambda r: r.timestamp)

    vectors: list[FeatureVector] = []
    for rec in sorted_records:
        fv = extractor.process(rec)
        if fv is not None:
            vectors.append(fv)

    print(f"  Built {len(vectors)} real FeatureVectors from {len(records)} records.")
    return vectors


# ── Synthetic data generator with real-world baselines ────────────────────────

def generate_synthetic_vectors(n_samples: int = 5000) -> list[FeatureVector]:
    """
    Generate synthetic FeatureVectors calibrated to real internet latency
    (~150–290 ms) rather than the original simulated baseline (~45 ms).

    Normal samples (~95%): latency mean 0.21, jitter mean 0.12.
    Anomaly samples (~5%): SPIKE / LOSS / DNS / CASCADE patterns.
    """
    rng = np.random.default_rng(seed=99)
    vectors: list[FeatureVector] = []
    anomaly_rate = 0.05
    anomaly_types = ["SPIKE", "LOSS", "DNS", "CASCADE"]

    for i in range(n_samples):
        is_anomaly = rng.random() < anomaly_rate
        atype = random.choice(anomaly_types) if is_anomaly else None

        # ── Normal real-world baseline ────────────────────────────────────────
        rm_lat    = float(np.clip(rng.normal(NORMAL_LAT_MEAN, NORMAL_LAT_STD), 0.0, 1.0))
        rs_lat    = float(np.clip(rng.normal(0.035, 0.010), 0.0, 1.0))
        rm_loss   = float(np.clip(rng.exponential(NORMAL_LOSS_MEAN), 0.0, 1.0))
        rs_loss   = float(np.clip(rng.normal(0.002, 0.001), 0.0, 1.0))
        rm_dns    = float(np.clip(rng.uniform(0.001, NORMAL_DNS_MEAN * 2.5), 0.0, 1.0))
        rm_jitter = float(np.clip(rng.normal(NORMAL_JITTER_MEAN, NORMAL_JITTER_STD), 0.0, 1.0))
        rs_jitter = float(np.clip(rng.normal(0.04, 0.015), 0.0, 1.0))

        lat_trend    = float(rng.normal(0.0, 0.001))
        health_trend = float(rng.normal(0.0, 0.001))
        spike_count  = float(max(0, int(rng.poisson(0.05))))
        loss_spike_count = float(max(0, int(rng.poisson(0.02))))
        lat_delta  = float(rng.normal(0.0, 0.006))
        loss_delta = float(rng.normal(0.0, 0.001))
        dns_delta  = float(rng.normal(0.0, 0.002))

        # Cyclic time features
        hour    = rng.integers(0, 24)
        weekday = rng.integers(0, 7)
        hour_sin = math.sin(2.0 * math.pi * hour    / 24.0)
        hour_cos = math.cos(2.0 * math.pi * hour    / 24.0)
        day_sin  = math.sin(2.0 * math.pi * weekday / 7.0)
        day_cos  = math.cos(2.0 * math.pi * weekday / 7.0)
        is_biz   = 1.0 if (weekday < 5 and 8 <= hour < 18) else 0.0

        # ── Anomaly overrides ─────────────────────────────────────────────────
        if atype == "SPIKE":
            rm_lat      = float(np.clip(rng.uniform(SPIKE_LAT_MIN, SPIKE_LAT_MAX), 0.0, 1.0))
            lat_trend   = float(rng.uniform(0.005, 0.020))
            spike_count = float(rng.integers(8, 20))

        elif atype == "LOSS":
            rm_loss           = float(np.clip(rng.uniform(LOSS_LOSS_MIN, LOSS_LOSS_MAX), 0.0, 1.0))
            loss_spike_count  = float(rng.integers(8, 20))

        elif atype == "DNS":
            rm_dns    = float(np.clip(rng.uniform(DNS_DNS_MIN, DNS_DNS_MAX), 0.0, 1.0))
            dns_delta = float(rng.uniform(0.05, 0.20))

        elif atype == "CASCADE":
            rm_lat       = float(np.clip(rng.uniform(SPIKE_LAT_MIN, SPIKE_LAT_MAX), 0.0, 1.0))
            rm_loss      = float(np.clip(rng.uniform(LOSS_LOSS_MIN, LOSS_LOSS_MAX), 0.0, 1.0))
            rm_dns       = float(np.clip(rng.uniform(DNS_DNS_MIN, DNS_DNS_MAX), 0.0, 1.0))
            rm_jitter    = float(np.clip(rng.uniform(0.40, 0.80), 0.0, 1.0))
            spike_count  = float(rng.integers(8, 20))
            loss_spike_count = float(rng.integers(8, 20))
            lat_trend    = float(rng.uniform(0.005, 0.020))
            health_trend = float(rng.uniform(0.005, 0.015))

        # Build a synthetic raw window matching the baseline means
        # (used by LSTM to_lstm_input() — columns: lat, loss, dns, jitter, health)
        w_lat    = np.clip(rng.normal(rm_lat,    rs_lat    + 1e-9, WINDOW_SIZE), 0.0, 1.0)
        w_loss   = np.clip(rng.normal(rm_loss,   rs_loss   + 1e-9, WINDOW_SIZE), 0.0, 1.0)
        w_dns    = np.clip(rng.normal(rm_dns,    0.005,             WINDOW_SIZE), 0.0, 1.0)
        w_jitter = np.clip(rng.normal(rm_jitter, rs_jitter + 1e-9, WINDOW_SIZE), 0.0, 1.0)
        w_health = (
            0.40 * w_lat + 0.30 * w_loss + 0.20 * w_dns + 0.10 * w_jitter
        )
        raw_window = np.column_stack([w_lat, w_loss, w_dns, w_jitter, w_health])

        ts       = f"2026-04-08T{hour:02d}:00:00+00:00"
        host_id  = random.choice(["google.com", "github.com", "cloudflare.com"])

        vectors.append(
            FeatureVector(
                host_id=host_id,
                timestamp=ts,
                rolling_mean_latency=rm_lat,
                rolling_std_latency=rs_lat,
                rolling_mean_loss=rm_loss,
                rolling_std_loss=rs_loss,
                rolling_mean_dns=rm_dns,
                rolling_mean_jitter=rm_jitter,
                rolling_std_jitter=rs_jitter,
                latency_trend=lat_trend,
                health_score_trend=health_trend,
                spike_count=spike_count,
                loss_spike_count=loss_spike_count,
                latency_delta=lat_delta,
                loss_delta=loss_delta,
                dns_delta=dns_delta,
                hour_sin=hour_sin,
                hour_cos=hour_cos,
                day_sin=day_sin,
                day_cos=day_cos,
                is_business_hours=is_biz,
                _raw_window=raw_window,
            )
        )

    n_anom = sum(1 for v in vectors if v.spike_count > 5 or v.loss_spike_count > 5)
    print(f"  Generated {len(vectors)} synthetic vectors ({n_anom} anomalies, "
          f"{n_anom/len(vectors):.1%} rate).")
    return vectors


# ── Training drivers ───────────────────────────────────────────────────────────

def retrain_isolation_forest(vectors: list[FeatureVector]) -> None:
    print("\n[IF] Retraining Isolation Forest...")
    detector = IsolationForestDetector(contamination=0.05, n_estimators=200)
    result = detector.train(vectors)
    print(
        f"  Done | version={result.model_version} "
        f"n_samples={result.n_samples} "
        f"anomaly_rate={result.training_anomaly_rate:.2%} "
        f"duration={result.training_duration_seconds:.1f}s"
    )


def retrain_lstm(vectors: list[FeatureVector]) -> None:
    print("\n[LSTM] Retraining LSTM autoencoder...")
    trainer = LSTMTrainer(epochs=50, patience=10, batch_size=64)
    result = trainer.train(vectors)
    print(
        f"  Done | version={result.model_version} "
        f"epochs={result.epochs_trained} "
        f"best_val_loss={result.best_val_loss:.6f} "
        f"threshold={result.threshold:.6f} "
        f"duration={result.training_duration_seconds:.1f}s"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Retrain FlowWatch models on real-world data")
    parser.add_argument("--synthetic-only", action="store_true",
                        help="Skip API fetch, use only synthetic data")
    parser.add_argument("--n-synthetic", type=int, default=6000,
                        help="Number of synthetic samples to generate (default: 6000)")
    args = parser.parse_args()

    print("=" * 60)
    print("FlowWatch — Model Retraining (real-world baselines)")
    print("=" * 60)

    all_vectors: list[FeatureVector] = []

    # ── Step 1: real data from API ────────────────────────────────────────────
    if not args.synthetic_only:
        print("\n[1/3] Fetching real telemetry from API...")
        real_records = fetch_real_records()
        if real_records:
            real_vectors = build_feature_vectors_from_records(real_records)
            all_vectors.extend(real_vectors)
        else:
            print("  No real data fetched — proceeding with synthetic only.")
    else:
        print("\n[1/3] Skipping API fetch (--synthetic-only).")

    # ── Step 2: synthetic data with real-world baselines ─────────────────────
    print(f"\n[2/3] Generating {args.n_synthetic} synthetic vectors...")
    synthetic_vectors = generate_synthetic_vectors(n_samples=args.n_synthetic)
    all_vectors.extend(synthetic_vectors)

    print(f"\n  Total training vectors: {len(all_vectors)}")
    real_count = len(all_vectors) - len(synthetic_vectors)
    print(f"    Real: {real_count}  |  Synthetic: {len(synthetic_vectors)}")

    if len(all_vectors) < 50:
        print("[ERROR] Not enough vectors to train. Aborting.")
        sys.exit(1)

    # ── Step 3: retrain both models ───────────────────────────────────────────
    print("\n[3/3] Retraining models...")
    retrain_isolation_forest(all_vectors)
    retrain_lstm(all_vectors)

    print("\n" + "=" * 60)
    print("Retraining complete!")
    print("Artifacts saved to backend/models/artifacts/")
    print()
    print("Restart the backend to apply:")
    print("  docker-compose -f infra/docker-compose.yml restart backend")
    print("=" * 60)


if __name__ == "__main__":
    main()
