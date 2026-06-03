#!/usr/bin/env python
"""
cost_model.py - Cost Estimation Model for Build vs Buy
Estimates the monthly cost of self-hosted observability pipelines (Build) 
vs Datadog SaaS (Buy) for Small, Medium, and Large tiers.
"""

import sys

def calculate_costs():
    # Define Tiers
    tiers = {
        "Small": {
            "services": 10,
            "hosts": 15,          # Assumed 1.5 hosts per service
            "log_gb_day": 50,
            "metric_eps": 100000, # 100K events/sec
            "custom_metrics": 20000, # 20K active timeseries
            "sre_fraction": 0.2    # 0.2 SRE workload
        },
        "Medium": {
            "services": 100,
            "hosts": 150,
            "log_gb_day": 500,
            "metric_eps": 1000000, # 1M events/sec
            "custom_metrics": 150000, # 150K active timeseries
            "sre_fraction": 1.0     # 1.0 SRE workload
        },
        "Large": {
            "services": 1000,
            "hosts": 1500,
            "log_gb_day": 5000,    # 5 TB/day
            "metric_eps": 10000000, # 10M events/sec
            "custom_metrics": 1500000, # 1.5M active timeseries
            "sre_fraction": 3.0      # 3.0 SREs workload
        }
    }

    # AWS Infrastructure Pricing
    EBS_GP3_GB_MONTH = 0.08
    S3_STANDARD_GB_MONTH = 0.023
    NETWORK_AZ_EGRESS_GB = 0.01
    SRE_SALARY_MONTH = 10000.0  # Fully loaded SRE monthly cost

    # Self-Hosted (Build) Compute VM Specifications & Costs
    # We size compute VMs based on data processing requirements for each tier
    build_compute_costs = {
        "Small": {
            "Kafka": 3 * 30,         # 3x t3.medium ($30 each)
            "Flink": 2 * 60,         # 2x t3.large ($60 each)
            "VictoriaMetrics": 1 * 30, # 1x t3.medium
            "Loki": 1 * 30,          # 1x t3.medium
            "Elasticsearch": 3 * 30, # 3x t3.medium
            "Collectors/Misc": 2 * 15 # 2x t3.small
        },
        "Medium": {
            "Kafka": 3 * 110,        # 3x m5.large ($110 each)
            "Flink": 4 * 120,        # 4x c5.xlarge ($120 each)
            "VictoriaMetrics": 2 * 110, # 2x m5.large
            "Loki": 2 * 110,         # 2x m5.large
            "Elasticsearch": 3 * 180, # 3x m5.xlarge ($180 each)
            "Collectors/Misc": 4 * 30 # 4x t3.medium
        },
        "Large": {
            "Kafka": 6 * 440,        # 6x m5.2xlarge ($440 each)
            "Flink": 12 * 240,       # 12x c5.2xlarge ($240 each)
            "VictoriaMetrics": 4 * 440, # 4x m5.2xlarge
            "Loki": 4 * 440,         # 4x m5.2xlarge
            "Elasticsearch": 6 * 500, # 6x r5.2xlarge ($500 each)
            "Collectors/Misc": 10 * 120 # 10x c5.xlarge
        }
    }

    # Datadog (Buy) Pricing Rates
    DD_INFRA_HOST_MONTH = 18.0     # Enterprise/Pro infra monitoring
    DD_APM_HOST_MONTH = 31.0       # APM + profiling license
    DD_LOG_INGEST_GB = 0.10        # Ingestion cost per GB
    DD_LOG_RETENTION_GB = 1.70     # 30-day index retention cost per GB
    DD_LOG_TOTAL_GB = DD_LOG_INGEST_GB + DD_LOG_RETENTION_GB
    DD_CUSTOM_METRICS_MONTH = 0.05 # Cost per active custom metric timeseries

    results = {}

    for name, spec in tiers.items():
        # --- 1. BUILD MODEL CALCULATIONS ---
        # Compute cost
        compute_breakdown = build_compute_costs[name]
        compute_total = sum(compute_breakdown.values())

        # Storage cost (Tiered: 7d ES Hot on EBS, 23d Loki Warm on S3, VM metrics on EBS)
        # Logs sizing:
        total_log_gb_month = spec["log_gb_day"] * 30
        # ES Hot (7 days): Log expands 1.3x due to indexes
        es_hot_gb = (spec["log_gb_day"] * 7) * 1.3
        es_hot_cost = es_hot_gb * EBS_GP3_GB_MONTH
        # Loki S3 (23 days): Loki compresses logs ~5x
        loki_warm_gb = (spec["log_gb_day"] * 23) / 5.0
        loki_warm_cost = loki_warm_gb * S3_STANDARD_GB_MONTH
        
        # Metrics sizing (VictoriaMetrics): 100 bytes raw payload per event.
        # VM compresses to 0.7 bytes per sample.
        total_samples_month = spec["metric_eps"] * 86400 * 30
        vm_storage_gb = (total_samples_month * 0.7) / (1024**3)
        vm_storage_cost = vm_storage_gb * EBS_GP3_GB_MONTH
        
        storage_total = es_hot_cost + loki_warm_cost + vm_storage_cost

        # Network cost (Cross-AZ replication for Kafka & ES data)
        # Metric raw size: ~100 bytes raw JSON payload
        metric_gb_day = (spec["metric_eps"] * 100 * 86400) / (1024**3)
        total_data_gb_month = (spec["log_gb_day"] + metric_gb_day) * 30
        # Assume HA replication multiplies cross-AZ network traffic by 1.5x
        network_total = (total_data_gb_month * 1.5) * NETWORK_AZ_EGRESS_GB

        # Labor cost
        labor_total = spec["sre_fraction"] * SRE_SALARY_MONTH

        build_total = compute_total + storage_total + network_total + labor_total

        # --- 2. BUY (DATADOG) MODEL CALCULATIONS ---
        dd_infra = spec["hosts"] * DD_INFRA_HOST_MONTH
        dd_apm = spec["hosts"] * DD_APM_HOST_MONTH
        dd_logs = total_log_gb_month * DD_LOG_TOTAL_GB
        # Custom metrics: Datadog includes 100 custom metrics per host free.
        free_metrics = spec["hosts"] * 100
        billable_metrics = max(0, spec["custom_metrics"] - free_metrics)
        dd_metrics = billable_metrics * DD_CUSTOM_METRICS_MONTH

        buy_total = dd_infra + dd_apm + dd_logs + dd_metrics

        # Save results
        results[name] = {
            "spec": spec,
            "build": {
                "compute": compute_total,
                "storage": storage_total,
                "network": network_total,
                "labor": labor_total,
                "total": build_total,
                "compute_breakdown": compute_breakdown,
                "storage_details": {
                    "es_hot_gb": es_hot_gb,
                    "es_hot_cost": es_hot_cost,
                    "loki_warm_gb": loki_warm_gb,
                    "loki_warm_cost": loki_warm_cost,
                    "vm_gb": vm_storage_gb,
                    "vm_cost": vm_storage_cost
                }
            },
            "buy": {
                "infra": dd_infra,
                "apm": dd_apm,
                "logs": dd_logs,
                "metrics": dd_metrics,
                "total": buy_total
            }
        }

    # Print results in Markdown Format
    print("# AIOps Telemetry Pipeline Cost Model Comparison\n")
    print(f"Generated at 2026-06-03 (Local time: 14:05)\n")

    for tier_name, data in results.items():
        spec = data["spec"]
        b = data["build"]
        d = data["buy"]
        
        print(f"## {tier_name} Tier Comparison")
        print(f"- **Scale**: {spec['services']} Services, {spec['hosts']} Hosts")
        print(f"- **Log Volume**: {spec['log_gb_day']} GB/day ({spec['log_gb_day']*30/1024:.2f} TB/month)")
        print(f"- **Metrics EPS**: {spec['metric_eps']:,} EPS ({spec['custom_metrics']:,} Custom Metrics)")
        print(f"- **Labor required**: {spec['sre_fraction']} SRE workload\n")

        print("| Component | Build (Self-Hosted Cloud) | Buy (Datadog SaaS) | Breakdown / Details |")
        print("| :--- | :---: | :---: | :--- |")
        
        # Row 1: Compute / Infrastructure Licenses
        print(f"| **Compute / Licenses** | ${b['compute']:,.2f} | ${d['infra'] + d['apm']:,.2f} | **Build**: VMs for Kafka, Flink, ES, VM, Loki<br>**Buy**: DD Host (${d['infra']:,.0f}) + APM (${d['apm']:,.0f}) |")
        
        # Row 2: Storage / Log Ingest
        print(f"| **Storage / Log Indexing** | ${b['storage']:,.2f} | ${d['logs']:,.2f} | **Build**: EBS GP3 for ES/VM + S3 for Loki<br>**Buy**: Ingest & Indexing ($1.80/GB) |")
        
        # Row 3: Network / Custom Metrics
        print(f"| **Network / Metrics Egress** | ${b['network']:,.2f} | ${d['metrics']:,.2f} | **Build**: Cross-AZ Kafka/ES replication<br>**Buy**: Custom Metrics billing |")
        
        # Row 4: Labor / Operations
        print(f"| **Labor / Maintenance** | ${b['labor']:,.2f} | $0.00 | **Build**: SRE payroll overhead<br>**Buy**: Zero maintenance overhead |")
        
        # Row 5: Total
        print(f"| **TOTAL** | **${b['total']:,.2f}** | **${d['total']:,.2f}** | **Difference**: **${abs(b['total'] - d['total']):,.2f}** ({'Build is cheaper' if b['total'] < d['total'] else 'Buy is cheaper'}) |")
        print("\n")

        # Recommendation section
        diff = b['total'] - d['total']
        print("### Recommendation:")
        if diff > 0:
            print(f"At **{tier_name}** scale, **BUY (Datadog)** is more cost-effective by **${diff:,.2f}/month** when SRE labor costs are accounted for. It frees up engineering resources to focus on product features.")
        else:
            print(f"At **{tier_name}** scale, **BUILD (Self-Hosted)** is significantly cheaper by **${abs(diff):,.2f}/month** (saving ~{abs(diff)/d['total']*100:.1f}%). The scale justifies dedicated SREs to run the telemetry pipeline.")
        print("\n---\n")

    # Generate summary CSV/Markdown table
    print("## Summary Table (Build vs Buy)")
    print("| Tier | Build (Self-Hosted) | Buy (Datadog) | Monthly Savings | Recommended Choice |")
    print("| :--- | :---: | :---: | :---: | :--- |")
    for tier_name, data in results.items():
        b = data["build"]["total"]
        d = data["buy"]["total"]
        savings = abs(b - d)
        rec = "Buy (Datadog)" if b > d else "Build (Self-Hosted)"
        print(f"| {tier_name} | ${b:,.2f} | ${d:,.2f} | ${savings:,.2f} | **{rec}** |")

if __name__ == "__main__":
    calculate_costs()
