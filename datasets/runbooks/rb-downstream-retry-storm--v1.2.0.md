---
id: rb-downstream-retry-storm
version: 1.2.0
service: synthetic-orders
title: "Retry storm amplifying a downstream timeout"
---

## service

synthetic-orders

## symptom

Downstream request volume is several times the inbound request volume. Downstream latency is high and error rate climbs on both sides.

## precondition

The client retries failed downstream calls. Both inbound and downstream request rates are measurable.

## diagnosis

When downstream request volume exceeds inbound volume by a multiple close to the retry count, retries are amplifying the original failure. The downstream service is being overwhelmed by the retries rather than by real traffic. Check the retry configuration for a missing backoff or an unbounded attempt count.

## safe_action

Throttle the client's traffic to reduce the amplification while the retry policy is corrected. Do not restart the downstream service: it will be overwhelmed again immediately.

## rollback

Restore traffic after the retry policy has a bounded attempt count and exponential backoff.
