---
id: rb-lock-contention
version: 1.0.0
service: synthetic-orders
title: "Database lock contention"
---

## service

synthetic-orders

## symptom

Latency rises while database CPU stays low. Lock wait counters increase. A subset of queries times out.

## precondition

Lock wait statistics are available from the database.

## diagnosis

Rising latency with low CPU and growing lock waits indicates contention rather than load. Identify the transaction holding the lock. A long-running transaction that also performs external calls is a frequent cause.

## safe_action

Shorten the transaction boundary so the lock is held for less time. Killing the blocking transaction is a last resort and may leave partial work.

## rollback

Confirm lock wait counters return to baseline after the transaction boundary is narrowed.
