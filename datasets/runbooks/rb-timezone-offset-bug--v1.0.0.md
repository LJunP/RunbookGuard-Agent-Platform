---
id: rb-timezone-offset-bug
version: 1.0.0
service: synthetic-report
title: "Report window shifted by a timezone offset"
---

## service

synthetic-report

## symptom

Reports include or exclude records near the window boundary. The offset matches the difference between two timezones.

## precondition

Timestamps are stored in one timezone and the report window is computed in another.

## diagnosis

A boundary error whose size equals a timezone offset points at a timezone conversion rather than at the query. Check whether the window bounds and the stored timestamps use the same reference. Data volume being otherwise correct rules out a filter problem.

## safe_action

Correct the conversion so both sides use the same reference. No restart is needed if the offset is configuration.

## rollback

Regenerate the affected reports after the correction.
