---
id: rb-stale-feature-flag
version: 1.0.0
service: synthetic-checkout
title: "Behaviour change from a stale feature flag"
---

## service

synthetic-checkout

## symptom

Behaviour differs between instances with no corresponding deployment. Error rate is elevated on some instances only.

## precondition

Feature flags are fetched at startup and cached for the process lifetime.

## diagnosis

Divergent behaviour without a deployment points at configuration rather than code. Instances that started before a flag change keep the old value because the flag is cached at startup. Compare each instance's start time against the flag change time.

## safe_action

Restart the instances holding the stale value so they fetch the current flag. This is one of the few cases where a restart is the correct remedy.

## rollback

If the flag change itself was wrong, revert the flag; the caches will pick it up on the next restart.
