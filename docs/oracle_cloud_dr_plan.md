# Oracle Cloud Always Free Standby Disaster Recovery Architecture

## 1. Specifications & Free Tier Allocation
- **Provider**: Oracle Cloud Infrastructure (OCI) Always Free
- **Shape**: `VM.Standard.A1.Flex` (Ampere ARM64)
- **CPUs**: 4 OCPUs (4 Physical ARM Cores)
- **RAM**: 24 GB RAM
- **Boot Disk**: 150 GB NVMe SSD
- **Bandwidth**: 10 TB / Month Outbound Free
- **Public IP**: 1 Static Reserved IPv4
- **Cost**: 100% Free Forever

## 2. Disaster Recovery Topology
- **Normal State**:
  - Campus Master (`192.168.99.2`) + GPU1 (`192.168.99.3`) + GPU2 (`192.168.99.4`) handle production load.
  - WireGuard VPN tunnel links Master (`10.10.200.2`) to Oracle Cloud Standby (`10.10.200.1`).
  - Incremental WAL sync / Read Replica standby stream.
- **Failover State (Power / Internet Cut at Campus)**:
  - Cloudflare Health Check probes `https://hoodle.in/api/heartbeat` every 60s.
  - If 2 probes fail, Cloudflare automatically routes `hoodle.in` to Oracle Cloud Public IP.
  - Standby serves Graceful Maintenance & Read-Only mode (Option A) with zero data loss or merge conflicts.
- **Failback State (Power Restored)**:
  - Automated sync script re-verifies campus master, syncs recent changes, and flips Cloudflare back to campus cluster.
