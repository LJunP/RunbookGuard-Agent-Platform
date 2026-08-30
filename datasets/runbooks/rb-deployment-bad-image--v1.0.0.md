---
id: rb-deployment-bad-image
version: 1.0.0
service: synthetic-report
title: "Deployment referencing a missing image"
---

## service

synthetic-report

## symptom

New pods never become ready. Runtime events record an image pull failure. The previous version continues serving.

## precondition

A rolling deployment is in progress and the previous version is still available.

## diagnosis

Pods that never start, combined with an image pull failure event, point at the image reference rather than the application. Check whether the tag exists in the registry. No application logs will be present because the process never started.

## safe_action

Roll back the deployment to the previous version. The failure is in the deployment specification, not in the running service.

## rollback

Push the correct image, then redeploy. Confirm new pods reach ready state.
