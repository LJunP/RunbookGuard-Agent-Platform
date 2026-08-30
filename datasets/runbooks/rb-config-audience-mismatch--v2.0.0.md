---
id: rb-config-audience-mismatch
version: 2.0.0
service: synthetic-checkout
title: "Downstream 401 caused by a wrong audience in configuration"
---

## service

synthetic-checkout

## symptom

Error rate jumps sharply at the moment of a deployment. Logs show '401 from synthetic-auth' together with 'audience mismatch'. Latency stays near its baseline.

## precondition

The service authenticates to a downstream service using a token audience taken from configuration. The deployment record exposes which configuration keys changed.

## diagnosis

A step change in error rate that aligns exactly with a deployment points at that deployment. Check the changed configuration keys for anything related to authentication. Critically, verify the downstream service's own metrics: if its error rate and latency are unchanged, the downstream service is healthy and must not be restarted. The fault is in this service's configuration. This revision adds an explicit note: confirm the finding with at least two independent signals before proposing any action.

## safe_action

Roll back this service to the previous version so that the previous audience value is restored. Do not restart or roll back the downstream service.

## rollback

After rollback, confirm the 401 count returns to baseline. Then correct the audience value and redeploy.
