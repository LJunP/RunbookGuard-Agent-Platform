---
id: rb-idempotency-key-collision
version: 1.0.0
service: synthetic-notify
title: "Requests silently dropped by an idempotency key collision"
---

## service

synthetic-notify

## symptom

Some requests return a success response but produce no effect. The responses match earlier requests with different content.

## precondition

Write operations carry an idempotency key and the server replays the first result for repeats.

## diagnosis

A success response with no effect, matching an earlier different request, indicates two distinct requests shared an idempotency key. The server treated the second as a repeat and replayed the first result. Check whether the key derivation includes everything that distinguishes the requests. A key reused with different content is a conflict, not a repeat, and should be rejected rather than replayed.

## safe_action

Correct the key derivation so distinct requests get distinct keys. Do not disable idempotency: that reintroduces duplicate side effects.

## rollback

Not applicable; the fix is in the key derivation.
