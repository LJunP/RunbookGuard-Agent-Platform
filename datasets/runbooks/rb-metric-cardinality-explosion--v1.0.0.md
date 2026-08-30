---
id: rb-metric-cardinality-explosion
version: 1.0.0
service: synthetic-notify
title: "Monitoring degradation from metric cardinality"
---

## service

synthetic-notify

## symptom

Dashboards become slow or incomplete. The metrics backend reports high series counts. The service itself is healthy.

## precondition

Metrics carry labels derived from request data.

## diagnosis

Slow dashboards with a healthy service point at the monitoring pipeline rather than at the service. A label derived from unbounded request data, such as an identifier, creates one series per distinct value. Check recently added labels.

## safe_action

Remove the high-cardinality label. The service needs no change beyond its metric definitions.

## rollback

Not applicable; the label removal is the fix.
