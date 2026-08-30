---
id: rb-db-pool-exhaustion
version: 2.0.0
service: synthetic-orders
title: "Database connection pool exhaustion"
---

## service

synthetic-orders

## symptom

Request latency p99 rises above 3 seconds while error rate climbs past 10 percent. The db_pool_active gauge sits at db_pool_max and db_pool_wait_count grows steadily. Application logs contain 'connection pool exhausted' with a timeout after 5000ms.

## precondition

The service uses a fixed-size connection pool. A recent deployment is present within the last 30 minutes. Database CPU and slow-query counters are within their baseline.

## diagnosis

Compare the pool saturation timestamp against the most recent deployment time. When pool_active equals pool_max and the saturation begins shortly after a release, the release is the likely cause: a code path acquires a connection without releasing it. Confirm by checking whether wait_count grows monotonically rather than spiking. Database-side metrics staying normal rules out load on the database itself. This revision adds an explicit note: confirm the finding with at least two independent signals before proposing any action.

## safe_action

Roll back the service to the previous known-good version. Do not restart the database: the pool lives in the application process, so a database restart drops healthy connections without addressing the leak.

## rollback

Redeploy the previous version and confirm db_pool_active returns to its baseline within two minutes. If saturation persists after rollback, the leak predates the release and the pool size should be raised temporarily while the leak is located.
