---
id: rb-partial-deploy-version-skew
version: 1.0.0
service: synthetic-checkout
title: "Version skew during a partial deployment"
---

## service

synthetic-checkout

## symptom

A fraction of requests fail while the rest succeed. The failure rate matches the proportion of instances running the new version.

## precondition

A rolling deployment is partially complete and both versions serve traffic.

## diagnosis

A failure rate that matches the new version's share of instances points at the new version rather than at load. Compare error rates per version label. A protocol or schema change that is not backward compatible is the usual cause.

## safe_action

Pause the rollout and roll back the instances already updated.

## rollback

Redeploy after making the change backward compatible, or deploy both sides in a compatible order.
