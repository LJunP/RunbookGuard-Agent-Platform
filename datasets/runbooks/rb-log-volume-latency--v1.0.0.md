---
id: rb-log-volume-latency
version: 1.0.0
service: synthetic-report
title: "Latency caused by synchronous logging"
---

## service

synthetic-report

## symptom

Latency correlates with log volume. Disk IO wait is elevated. CPU is not saturated.

## precondition

The service writes logs synchronously to disk on the request path.

## diagnosis

Latency that tracks log volume with elevated IO wait indicates the request path is blocked on log writes. Distinguish this from disk pressure: free space is adequate, the problem is throughput not capacity. Verbose logging enabled for debugging is a common trigger.

## safe_action

Reduce the log level to its normal value, or switch the appender to asynchronous. No restart is needed if the level is dynamically configurable.

## rollback

Restore the previous log level after the investigation that required it is complete.
