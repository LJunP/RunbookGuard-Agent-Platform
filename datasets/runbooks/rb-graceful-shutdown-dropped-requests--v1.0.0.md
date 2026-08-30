---
id: rb-graceful-shutdown-dropped-requests
version: 1.0.0
service: synthetic-checkout
title: "Requests dropped during shutdown"
---

## service

synthetic-checkout

## symptom

A burst of errors coincides exactly with a deployment's pod termination, then stops.

## precondition

The service receives a termination signal and the platform waits a grace period.

## diagnosis

Errors confined to the termination moment indicate in-flight requests were dropped rather than a problem with the new version. Check whether the service stops accepting new connections before finishing in-flight work, and whether the grace period exceeds the longest request.

## safe_action

Increase the grace period and implement connection draining. No rollback is needed: the new version itself is healthy.

## rollback

Not applicable; the change is to the shutdown behaviour.
