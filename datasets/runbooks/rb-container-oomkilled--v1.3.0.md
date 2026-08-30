---
id: rb-container-oomkilled
version: 1.3.0
service: synthetic-report
title: "Container repeatedly OOMKilled"
---

## service

synthetic-report

## symptom

Restart count climbs several times within ten minutes. Memory usage shows a sawtooth pattern that reaches the container memory limit before each restart. Runtime events record OOMKilled with exit code 137.

## precondition

The container has an explicit memory limit. Runtime events are queryable.

## diagnosis

Align the OOMKilled event timestamps with the memory usage peaks. When memory reaches the limit immediately before each restart and no application-level panic precedes it, the kernel terminated the process for exceeding its limit. Restarting is already happening automatically, so a restart is not a remedy: it is the symptom. Look for a workload change that increased peak memory, such as a larger report range.

## safe_action

Raise the memory limit as a temporary measure, or reduce the batch size of the workload. Do not issue a manual restart: the container is already restarting on its own.

## rollback

Revert the limit change once the memory growth is fixed in code. Confirm the sawtooth no longer reaches the limit.
