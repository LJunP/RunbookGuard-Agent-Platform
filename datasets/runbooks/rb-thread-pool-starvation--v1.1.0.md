---
id: rb-thread-pool-starvation
version: 1.1.0
service: synthetic-checkout
title: "Thread pool starvation from blocking calls"
---

## service

synthetic-checkout

## symptom

Request queue length grows while CPU utilisation stays low. Latency rises for all requests.

## precondition

The service uses a bounded thread pool for request handling.

## diagnosis

A growing queue with low CPU means threads are blocked rather than busy. Look for synchronous calls inside handlers that should be asynchronous, or for a lock held across an external call. Thread dumps show most threads in a waiting state.

## safe_action

Reduce the blocking call's timeout so threads are released sooner, as a temporary mitigation. Increasing the pool size hides the problem and raises memory usage.

## rollback

Restore the original timeout once the blocking call is made asynchronous.
