---
id: rb-memory-leak-slow
version: 1.0.0
service: synthetic-cart
title: "Slow memory leak without OOM"
---

## service

synthetic-cart

## symptom

Memory usage grows steadily over hours without reaching the limit. Latency degrades gradually. No restarts occur.

## precondition

Memory metrics cover a long enough window to show the trend.

## diagnosis

Steady growth without a sawtooth and without restarts distinguishes a slow leak from the OOMKilled pattern. Because the limit is not reached, the kernel does not intervene and the symptom is degradation rather than restarts. Look for an unbounded cache or a collection that is appended to but never trimmed.

## safe_action

Restart the service to reclaim memory as a temporary mitigation, noting that this does not fix the leak and the growth will resume.

## rollback

No rollback applies; the fix is in code. Confirm the growth rate falls after the fix.
