---
id: rb-rate-limit-false-positive
version: 1.0.0
service: synthetic-auth
title: "Legitimate traffic rejected by a rate limiter"
---

## service

synthetic-auth

## symptom

A subset of clients receives 429 responses while total request volume is well below capacity. The affected clients share a network origin.

## precondition

Rate limiting is keyed by client identity or source address.

## diagnosis

429 responses at low total volume indicate the limiter's key is too coarse rather than genuine overload. Clients sharing a network origin behind a shared address all count against one bucket. Check the limiter's key derivation.

## safe_action

Widen the limiter's key to include a per-client identifier. Raising the global limit would let a genuinely abusive client through.

## rollback

Restore the original limit value after the key is corrected.
