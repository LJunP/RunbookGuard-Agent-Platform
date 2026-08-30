---
id: rb-cpu-throttling
version: 1.0.0
service: synthetic-report
title: "CPU throttling caused by a low limit"
---

## service

synthetic-report

## symptom

Latency rises while CPU utilisation appears to plateau below the limit. Throttled period counters increase.

## precondition

The container has a CPU limit and throttling metrics are available.

## diagnosis

A utilisation plateau below the limit combined with rising throttled periods means the workload is being throttled within each scheduling period rather than being starved overall. Average utilisation hides this; look at the throttling counter directly.

## safe_action

Raise the CPU limit or reduce the per-request work. No restart is needed.

## rollback

Revert the limit change after the workload is optimised. Confirm throttled periods return to zero.
