---
id: rb-schema-migration-lock
version: 1.0.0
service: synthetic-orders
title: "Write outage during a schema migration"
---

## service

synthetic-orders

## symptom

Writes fail while reads succeed, starting when a migration began. The affected table is the one being altered.

## precondition

A schema migration is running against a table the service writes to.

## diagnosis

Writes failing on exactly the table under migration, starting at the migration's start time, identifies the migration as the cause. Distinguish this from lock contention between application transactions: here the lock holder is the migration, not a request.

## safe_action

Wait for the migration to complete if it is progressing. Killing a migration partway can leave the schema in an intermediate state.

## rollback

If the migration must be stopped, follow its documented rollback rather than terminating the process.
