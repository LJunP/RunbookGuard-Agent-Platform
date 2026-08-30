---
id: rb-orphaned-transaction
version: 1.0.0
service: synthetic-orders
title: "Orphaned transaction holding resources after a crash"
---

## service

synthetic-orders

## symptom

Resource counters do not return to baseline after a process restart. Lock waits persist with no corresponding active request.

## precondition

The service participates in transactions that can outlive a single request.

## diagnosis

Resources held with no active request indicate a transaction that was never committed or rolled back, typically because the process died between the commit call and its acknowledgement. This is the timeout-after-commit shape: the caller does not know whether the work completed.

## safe_action

Identify the orphaned transaction and roll it back only after confirming from the audit trail whether its effect took hold. Rolling back committed work causes data loss.

## rollback

Not applicable; the remedy is determined case by case from the audit trail.
