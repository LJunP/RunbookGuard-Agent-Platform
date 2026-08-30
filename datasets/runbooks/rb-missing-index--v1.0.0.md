---
id: rb-missing-index
version: 1.0.0
service: synthetic-orders
title: "Slow query caused by a missing index"
---

## service

synthetic-orders

## symptom

One query's latency is far above the rest. Database CPU rises with request volume. Rows examined greatly exceeds rows returned.

## precondition

Query statistics including rows examined are available.

## diagnosis

A large gap between rows examined and rows returned indicates a full scan. Identify the query and check whether its filter columns are indexed. Rising database CPU that tracks request volume distinguishes this from a lock contention problem, where CPU would stay low.

## safe_action

Add the missing index during a low-traffic window. No service restart is required.

## rollback

Drop the index only if it proves unused; confirm rows examined falls close to rows returned.
