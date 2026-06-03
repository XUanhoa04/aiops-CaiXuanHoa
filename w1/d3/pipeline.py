#!/usr/bin/env python
# /// script
# dependencies = [
#   "pandas",
#   "pyarrow",
# ]
# ///
"""
pipeline.py - Mock Streaming Pipeline
This script simulates a streaming telemetry pipeline like Flink/Spark Streaming.
It reads machine temperature data, streams it through a thread-safe queue using
a Producer thread, and extracts rolling window features (rolling mean, rolling std,
rate of change) in a Consumer thread.

Requirements:
- uv run python pipeline.py
"""

import os
import sys
import time
import queue
import math
import argparse
import pandas as pd
from datetime import datetime
from collections import deque
from threading import Thread

# 1-hour window for 5-minute granularity data = 60 / 5 = 12 data points
DEFAULT_WINDOW_SIZE = 12
DEFAULT_SLEEP_TIME = 0.0001  # Tiny sleep to allow thread context switching but run fast

class MockProducer(Thread):
    """
    Simulates a Kafka producer that reads a CSV source and pushes
    events onto a queue (representing a Kafka topic) row-by-row.
    """
    def __init__(self, csv_path: str, data_queue: queue.Queue, sleep_time: float):
        super().__init__()
        self.csv_path = csv_path
        self.queue = data_queue
        self.sleep_time = sleep_time
        self.row_count = 0

    def run(self):
        print(f"[PRODUCER] Reading data from {self.csv_path}...")
        try:
            # Read CSV chunk by chunk or using pandas for quick reading
            df = pd.read_csv(self.csv_path)
            total_rows = len(df)
            print(f"[PRODUCER] Found {total_rows} rows. Starting stream...")
            
            for _, row in df.iterrows():
                event = {
                    "timestamp": str(row["timestamp"]),
                    "value": float(row["value"])
                }
                self.queue.put(event)
                self.row_count += 1
                
                # Sleep a tiny bit to simulate streaming arrival
                if self.sleep_time > 0:
                    time.sleep(self.sleep_time)
                
                if self.row_count % 5000 == 0:
                    print(f"[PRODUCER] Sent {self.row_count}/{total_rows} events to queue...")
                    
            # Put sentinel to signal end of stream
            self.queue.put(None)
            print(f"[PRODUCER] Finished streaming. Total events sent: {self.row_count}")
        except Exception as e:
            print(f"[PRODUCER ERROR] Fail to stream CSV: {e}", file=sys.stderr)
            self.queue.put(None)  # Ensure consumer exits on error


class StreamingFeatureExtractor(Thread):
    """
    Simulates a stream processing engine (like Apache Flink) that consumes
    events from a queue, processes them sequentially, maintains state (sliding window),
    computes real-time features, and writes the output.
    """
    def __init__(self, data_queue: queue.Queue, window_size: int, output_parquet: str, output_json: str):
        super().__init__()
        self.queue = data_queue
        self.window_size = window_size
        self.output_parquet = output_parquet
        self.output_json = output_json
        
        # Sliding window buffer to maintain Flink-like state in-memory
        self.window = deque(maxlen=window_size)
        self.prev_value = None
        self.processed_records = []
        self.processed_count = 0

    def run(self):
        print(f"[CONSUMER] Initializing with window size: {self.window_size} (approx. {self.window_size * 5} mins)...")
        start_time = time.time()
        
        while True:
            event = self.queue.get()
            if event is None:
                self.queue.task_done()
                break
                
            ts = event["timestamp"]
            val = event["value"]
            
            # 1. Update sliding window state
            self.window.append(val)
            
            # 2. Compute Flink-like streaming features
            # Rolling Mean
            rolling_mean = sum(self.window) / len(self.window)
            
            # Rolling Std (sample standard deviation, require len > 1)
            if len(self.window) > 1:
                variance = sum((x - rolling_mean) ** 2 for x in self.window) / (len(self.window) - 1)
                rolling_std = math.sqrt(variance)
            else:
                rolling_std = 0.0
                
            # Rate of Change (current value - previous value)
            if self.prev_value is not None:
                rate_of_change = val - self.prev_value
            else:
                rate_of_change = 0.0
                
            # Update previous value for next event
            self.prev_value = val
            
            # 3. Store the enriched record
            self.processed_records.append({
                "timestamp": ts,
                "value": val,
                "rolling_mean": rolling_mean,
                "rolling_std": rolling_std,
                "rate_of_change": rate_of_change
            })
            
            self.processed_count += 1
            self.queue.task_done()
            
            if self.processed_count % 5000 == 0:
                elapsed = time.time() - start_time
                throughput = self.processed_count / elapsed if elapsed > 0 else 0
                print(f"[CONSUMER] Processed {self.processed_count} events... (Throughput: {throughput:.1f} events/s)")
                
        # Stream finished, write results
        elapsed_total = time.time() - start_time
        print(f"[CONSUMER] Processing completed in {elapsed_total:.2f}s. Saving features...")
        self.save_output()

    def save_output(self):
        df_features = pd.DataFrame(self.processed_records)
        
        # Save to Parquet format (primary requirement)
        parquet_saved = False
        try:
            df_features.to_parquet(self.output_parquet, index=False)
            print(f"[CONSUMER] Features successfully saved as Parquet: {self.output_parquet}")
            parquet_saved = True
        except ImportError as e:
            print(f"[CONSUMER WARNING] Parquet engine (pyarrow or fastparquet) is missing: {e}")
        except Exception as e:
            print(f"[CONSUMER ERROR] Failed to save Parquet file: {e}")
            
        # Save to JSON format (fallback / alternative requirement)
        try:
            df_features.to_json(self.output_json, orient="records", indent=2)
            print(f"[CONSUMER] Features successfully saved as JSON: {self.output_json}")
        except Exception as e:
            print(f"[CONSUMER ERROR] Failed to save JSON file: {e}")

        # Summary of features
        print("\n=== Feature Extraction Sample (Last 5 Rows) ===")
        print(df_features.tail(5).to_string(index=False))
        print("==============================================\n")


def find_csv_path(filename="machine_temperature_system_failure.csv") -> str:
    """
    Search in multiple candidate directories to find the target CSV file.
    """
    candidates = [
        os.path.join(".", "realKnownCause", filename),
        os.path.join(".", "d1", "data", filename),
        os.path.join("..", "d1", "data", filename),
        os.path.join("d:", os.sep, "DevopsAndCloud", "AIOPS", "W1", "d1", "data", filename),
        os.path.join("D:", os.sep, "DevopsAndCloud", "AIOPS", "W1", "d1", "data", filename),
    ]
    for path in candidates:
        if os.path.exists(path):
            return os.path.abspath(path)
            
    # Recursive search as fallback
    print(f"[SYSTEM] CSV not found in standard directories. Searching workspace...")
    for root, dirs, files in os.walk(os.path.abspath(".")):
        if filename in files:
            return os.path.join(root, filename)
    for root, dirs, files in os.walk(os.path.abspath("..")):
        if filename in files:
            return os.path.join(root, filename)
            
    raise FileNotFoundError(f"Could not locate the file {filename} in standard paths.")


def main():
    parser = argparse.ArgumentParser(description="Mock Streaming Data Pipeline")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW_SIZE, 
                        help=f"Rolling window size (default: {DEFAULT_WINDOW_SIZE})")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_TIME, 
                        help=f"Producer delay per row in seconds (default: {DEFAULT_SLEEP_TIME})")
    parser.add_argument("--output-parquet", type=str, default="features.parquet", 
                        help="Output Parquet filepath (default: features.parquet)")
    parser.add_argument("--output-json", type=str, default="features.json", 
                        help="Output JSON filepath (default: features.json)")
    args = parser.parse_args()

    # Find the data source path
    try:
        csv_path = find_csv_path()
    except FileNotFoundError as e:
        print(f"[FATAL ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    
    print("Starting AIOps d3 Mock Streaming Pipeline")
    
    # Thread-safe queue representing our Kafka Broker/Topic
    kafka_queue = queue.Queue(maxsize=5000)

    # Initialize threads
    producer = MockProducer(csv_path, kafka_queue, args.sleep)
    consumer = StreamingFeatureExtractor(
        data_queue=kafka_queue,
        window_size=args.window,
        output_parquet=os.path.join(".", args.output_parquet),
        output_json=os.path.join(".", args.output_json)
    )

    # Start threads
    producer.start()
    consumer.start()

    # Wait for completion
    producer.join()
    consumer.join()

    print("Streaming Pipeline completed successfully!")

if __name__ == "__main__":
    main()
