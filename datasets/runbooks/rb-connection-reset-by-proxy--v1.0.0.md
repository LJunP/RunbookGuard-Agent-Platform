---
id: rb-connection-reset-by-proxy
version: 1.0.0
service: synthetic-cart
title: "Connections reset by an intermediate proxy"
---

## service

synthetic-cart

## symptom

Application logs report database query timeouts, but database-side metrics show normal connection counts, normal CPU and no slow queries. The two signals contradict each other.

## precondition

Traffic between the application and the database passes through a proxy or a network layer that is not directly observable.

## diagnosis

When application logs claim the database is slow but the database's own metrics are healthy, one of the two signals must be wrong or something between them is at fault. Do not pick a side: state the contradiction explicitly and list what would settle it. Network-layer metrics between the two hosts are the missing evidence. Without them the correct conclusion is that the cause cannot be determined from the available signals.

## safe_action

No safe action can be taken until the contradiction is resolved. Restarting the database would be acting on the signal that the evidence contradicts.

## rollback

Not applicable; no change was made.
