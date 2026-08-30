---
id: rb-readiness-probe-failure
version: 1.1.0
service: synthetic-checkout
title: "Readiness probe failing while the process is alive"
---

## service

synthetic-checkout

## symptom

The service is removed from load balancing but the process keeps running. Readiness checks fail while liveness checks pass.

## precondition

Readiness and liveness probes are configured separately and check different things.

## diagnosis

A failing readiness probe with a passing liveness probe means the process is alive but not able to serve. The usual cause is a dependency the readiness check verifies, such as a database connection or a migration that has not completed. Read the readiness endpoint's own response rather than inferring from the probe result alone.

## safe_action

Fix the unmet dependency. Do not relax the readiness threshold to make the probe pass: that routes traffic to an instance that cannot serve it.

## rollback

Confirm readiness passes on its own once the dependency is available.
