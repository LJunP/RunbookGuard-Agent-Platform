---
id: rb-queue-poison-message
version: 1.0.0
service: synthetic-notify
title: "Poison message blocking a queue"
---

## service

synthetic-notify

## symptom

One message is redelivered repeatedly. Consumer error rate is elevated but throughput for other messages is normal. Dead letter queue depth grows.

## precondition

The consumer acknowledges messages manually and a dead letter queue is configured.

## diagnosis

Repeated redelivery of the same message with normal throughput otherwise indicates a message the consumer cannot process. Read the failing message's content. Distinguish this from a consumer-wide failure, where all messages would fail.

## safe_action

Let the delivery count reach its limit so the message moves to the dead letter queue, then inspect it there. Do not purge the main queue.

## rollback

Once the message format is handled, replay it from the dead letter queue.
