---
id: rb-redis-hot-key
version: 1.0.0
service: synthetic-cart
title: "Redis hot key causing uneven latency"
---

## service

synthetic-cart

## symptom

A small subset of requests shows very high latency while most stay fast. Cache hit ratio remains high overall. One key accounts for a disproportionate share of operations.

## precondition

The cache is shared across tenants and keys are derived from request parameters.

## diagnosis

Uneven latency with a healthy overall hit ratio suggests contention on a single key rather than a cache-wide problem. Identify the key with the highest operation count. A hot key usually indicates a missing per-entity partition in the key scheme or an unexpectedly popular entity.

## safe_action

Add jitter to the expiry of the hot key so that its refreshes spread out, or shard the key by a stable suffix. No service restart is required.

## rollback

Remove the jitter once the key scheme is partitioned. Confirm the per-key operation counts are evenly distributed.
