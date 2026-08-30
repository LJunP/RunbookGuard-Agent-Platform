---
id: rb-cache-stampede
version: 1.0.0
service: synthetic-cart
title: "Cache stampede after a mass expiry"
---

## service

synthetic-cart

## symptom

A sudden spike in database load coincides with a drop in cache hit ratio. Latency rises across all requests rather than a subset.

## precondition

Many cache entries were written at the same time and therefore expire at the same time.

## diagnosis

A simultaneous drop in hit ratio and rise in database load indicates that many entries expired together and every request went to the origin. Distinguish this from a hot key: a stampede affects all requests, a hot key affects a subset. Check whether the entries share a common write timestamp.

## safe_action

Throttle incoming traffic briefly so the cache can refill, then stagger the expiry of new entries.

## rollback

Restore full traffic once the hit ratio returns to baseline.
