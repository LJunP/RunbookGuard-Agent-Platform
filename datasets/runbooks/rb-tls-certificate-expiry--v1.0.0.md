---
id: rb-tls-certificate-expiry
version: 1.0.0
service: synthetic-auth
title: "TLS certificate expiry"
---

## service

synthetic-auth

## symptom

All outbound calls to one downstream fail at the same moment with a certificate validation error. No deployment preceded the failure.

## precondition

The downstream presents a TLS certificate with a fixed expiry.

## diagnosis

A simultaneous failure of every call with a certificate error, absent any deployment, points at expiry rather than at a code change. Check the certificate's not-after date against the failure timestamp.

## safe_action

Renew the certificate. Disabling verification is not an acceptable mitigation: it removes the protection the certificate provides.

## rollback

Confirm calls succeed after renewal. Add an expiry alert so the next renewal is not reactive.
