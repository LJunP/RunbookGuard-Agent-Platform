---
id: rb-disk-pressure
version: 1.0.0
service: synthetic-orders
title: "Disk pressure from unrotated logs"
---

## service

synthetic-orders

## symptom

Write operations begin failing while read operations succeed. Free disk space approaches zero.

## precondition

The service writes logs to a local volume and rotation is configured externally.

## diagnosis

Failing writes with succeeding reads point at the filesystem rather than the application. Check free space and the size of the log directory. Unrotated logs are a common cause when rotation is configured outside the application.

## safe_action

Rotate and compress the logs to reclaim space. Do not delete data directories.

## rollback

Confirm free space recovers and write operations succeed. Fix the rotation configuration so the situation does not recur.
