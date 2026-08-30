---
id: rb-undocumented-third-party-throttle
version: 1.0.0
service: synthetic-search
title: "Latency from an undocumented third-party throttle"
---

## service

synthetic-search

## symptom

Latency rises to several seconds while error rate stays normal and resource usage stays flat. No deployment occurred in the last day. Logs contain only generic 'upstream slow' messages with no specific cause.

## precondition

The service calls a third-party dependency whose internal behaviour is not observable.

## diagnosis

Rising latency with a normal error rate, flat resource usage and no recent deployment rules out load, capacity and code change. The remaining signals do not identify a cause. When the available evidence excludes the known possibilities without pointing at a specific one, the correct conclusion is that the cause is undetermined, together with a list of what additional evidence would settle it.

## safe_action

No action can be justified from the available evidence. Escalate with the exclusions already established so the next responder does not repeat them.

## rollback

Not applicable; no change was made.
