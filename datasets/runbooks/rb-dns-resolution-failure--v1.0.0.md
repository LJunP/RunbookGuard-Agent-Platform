---
id: rb-dns-resolution-failure
version: 1.0.0
service: synthetic-notify
title: "Intermittent DNS resolution failures"
---

## service

synthetic-notify

## symptom

Outbound calls fail intermittently with host-not-found errors. Retries often succeed.

## precondition

The service resolves downstream hostnames at call time.

## diagnosis

Intermittent host-not-found errors that succeed on retry point at name resolution rather than at the downstream service. Check the resolver's error rate and whether the failures cluster in time. A downstream outage would fail consistently, not intermittently.

## safe_action

Increase the resolver cache TTL as a mitigation so fewer lookups are made. No restart is needed.

## rollback

Restore the original TTL once the resolver is stable.
