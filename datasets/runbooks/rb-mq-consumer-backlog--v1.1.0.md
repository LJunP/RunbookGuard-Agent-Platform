---
id: rb-mq-consumer-backlog
version: 1.1.0
service: synthetic-notify
title: "Message queue backlog caused by slow consumers"
---

## service

synthetic-notify

## symptom

Queue depth grows monotonically and the age of the oldest message exceeds 900 seconds. Consumer throughput drops while message_process_duration_p99 rises.

## precondition

The queue has a stable set of consumers. Publish rate is measurable independently from deliver rate.

## diagnosis

The decisive question is whether the backlog comes from the producer or the consumer. Compare publish_rate against deliver_rate: if publish_rate is flat while deliver_rate falls, the consumer slowed down and the producer is not at fault. Look for downstream timeouts in consumer logs. A deployment close in time may be unrelated; check whether its changed configuration keys have anything to do with the consumer path.

## safe_action

Throttle the producer to stop the backlog from growing while the consumer issue is investigated. Do not purge the queue: the messages represent real work.

## rollback

Restore the producer rate to 100 percent once deliver_rate recovers above publish_rate and oldest_age_seconds begins falling.
