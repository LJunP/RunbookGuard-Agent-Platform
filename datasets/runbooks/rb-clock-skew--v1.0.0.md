---
id: rb-clock-skew
version: 1.0.0
service: synthetic-auth
title: "Token validation failing due to clock skew"
---

## service

synthetic-auth

## symptom

Token validation fails intermittently with 'not yet valid' or 'expired' errors even though the tokens were just issued.

## precondition

Tokens carry issued-at and expiry claims. The issuer and validator run on different hosts.

## diagnosis

Validation errors on freshly issued tokens point at a time difference between issuer and validator rather than at the tokens. Compare the two hosts' clocks. A skew larger than the allowed leeway explains intermittent failures.

## safe_action

Synchronise the clocks. Increasing the leeway is a temporary mitigation, not a fix, because it widens the window in which a replayed token stays valid.

## rollback

Reduce the leeway back to its original value after synchronisation is confirmed.
