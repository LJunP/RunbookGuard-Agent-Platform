---
id: rb-backup-job-contention
version: 1.0.0
service: synthetic-orders
title: "Latency spike during a scheduled backup"
---

## service

synthetic-orders

## symptom

Latency rises at the same time every day and returns to normal afterwards. Database IO utilisation peaks during the window.

## precondition

A backup job runs on a schedule against the same storage the service uses.

## diagnosis

A latency pattern that repeats at a fixed time of day points at a scheduled job rather than at traffic. Correlate the window with the backup schedule and with IO utilisation. Request volume staying flat during the window confirms it is not load.

## safe_action

Move the backup window outside peak hours, or throttle the backup's IO. No service change is required.

## rollback

Restore the original schedule only if the contention is addressed another way.
