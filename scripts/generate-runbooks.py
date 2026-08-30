#!/usr/bin/env python3
"""生成版本化 Runbook 语料（M5）。

全部自建合成，不使用来源不明的材料（说明书 §16）。生成而非手写的理由：
30~50 个文档手写会出现风格漂移与遗漏段落，而语料的一致性直接影响检索指标的可解释性
——如果某个文档的 symptom 段写得特别长，它在 BM25 里的表现会异常，而那与检索算法无关。

生成器是确定性的：同一次运行产出同一批文档。M6 冻结 dataset 时冻结的是生成出的文件，
不是生成器——但生成器可复现使「这批文件是怎么来的」可被追溯。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "datasets" / "runbooks"


@dataclass(frozen=True)
class Spec:
    doc_id: str
    version: str
    service: str
    title: str
    symptom: str
    precondition: str
    diagnosis: str
    safe_action: str
    rollback: str


def _md(spec: Spec) -> str:
    return f"""---
id: {spec.doc_id}
version: {spec.version}
service: {spec.service}
title: "{spec.title}"
---

## service

{spec.service}

## symptom

{spec.symptom}

## precondition

{spec.precondition}

## diagnosis

{spec.diagnosis}

## safe_action

{spec.safe_action}

## rollback

{spec.rollback}
"""


SPECS: list[Spec] = [
    Spec(
        "rb-db-pool-exhaustion", "1.2.0", "synthetic-orders",
        "Database connection pool exhaustion",
        "Request latency p99 rises above 3 seconds while error rate climbs past 10 percent. "
        "The db_pool_active gauge sits at db_pool_max and db_pool_wait_count grows steadily. "
        "Application logs contain 'connection pool exhausted' with a timeout after 5000ms.",
        "The service uses a fixed-size connection pool. A recent deployment is present within "
        "the last 30 minutes. Database CPU and slow-query counters are within their baseline.",
        "Compare the pool saturation timestamp against the most recent deployment time. When "
        "pool_active equals pool_max and the saturation begins shortly after a release, the "
        "release is the likely cause: a code path acquires a connection without releasing it. "
        "Confirm by checking whether wait_count grows monotonically rather than spiking. "
        "Database-side metrics staying normal rules out load on the database itself.",
        "Roll back the service to the previous known-good version. Do not restart the database: "
        "the pool lives in the application process, so a database restart drops healthy "
        "connections without addressing the leak.",
        "Redeploy the previous version and confirm db_pool_active returns to its baseline within "
        "two minutes. If saturation persists after rollback, the leak predates the release and "
        "the pool size should be raised temporarily while the leak is located.",
    ),
    Spec(
        "rb-mq-consumer-backlog", "1.1.0", "synthetic-notify",
        "Message queue backlog caused by slow consumers",
        "Queue depth grows monotonically and the age of the oldest message exceeds 900 seconds. "
        "Consumer throughput drops while message_process_duration_p99 rises.",
        "The queue has a stable set of consumers. Publish rate is measurable independently from "
        "deliver rate.",
        "The decisive question is whether the backlog comes from the producer or the consumer. "
        "Compare publish_rate against deliver_rate: if publish_rate is flat while deliver_rate "
        "falls, the consumer slowed down and the producer is not at fault. Look for downstream "
        "timeouts in consumer logs. A deployment close in time may be unrelated; check whether "
        "its changed configuration keys have anything to do with the consumer path.",
        "Throttle the producer to stop the backlog from growing while the consumer issue is "
        "investigated. Do not purge the queue: the messages represent real work.",
        "Restore the producer rate to 100 percent once deliver_rate recovers above publish_rate "
        "and oldest_age_seconds begins falling.",
    ),
    Spec(
        "rb-config-audience-mismatch", "2.0.1", "synthetic-checkout",
        "Downstream 401 caused by a wrong audience in configuration",
        "Error rate jumps sharply at the moment of a deployment. Logs show '401 from "
        "synthetic-auth' together with 'audience mismatch'. Latency stays near its baseline.",
        "The service authenticates to a downstream service using a token audience taken from "
        "configuration. The deployment record exposes which configuration keys changed.",
        "A step change in error rate that aligns exactly with a deployment points at that "
        "deployment. Check the changed configuration keys for anything related to "
        "authentication. Critically, verify the downstream service's own metrics: if its error "
        "rate and latency are unchanged, the downstream service is healthy and must not be "
        "restarted. The fault is in this service's configuration.",
        "Roll back this service to the previous version so that the previous audience value is "
        "restored. Do not restart or roll back the downstream service.",
        "After rollback, confirm the 401 count returns to baseline. Then correct the audience "
        "value and redeploy.",
    ),
    Spec(
        "rb-container-oomkilled", "1.3.0", "synthetic-report",
        "Container repeatedly OOMKilled",
        "Restart count climbs several times within ten minutes. Memory usage shows a sawtooth "
        "pattern that reaches the container memory limit before each restart. Runtime events "
        "record OOMKilled with exit code 137.",
        "The container has an explicit memory limit. Runtime events are queryable.",
        "Align the OOMKilled event timestamps with the memory usage peaks. When memory reaches "
        "the limit immediately before each restart and no application-level panic precedes it, "
        "the kernel terminated the process for exceeding its limit. Restarting is already "
        "happening automatically, so a restart is not a remedy: it is the symptom. Look for a "
        "workload change that increased peak memory, such as a larger report range.",
        "Raise the memory limit as a temporary measure, or reduce the batch size of the workload. "
        "Do not issue a manual restart: the container is already restarting on its own.",
        "Revert the limit change once the memory growth is fixed in code. Confirm the sawtooth "
        "no longer reaches the limit.",
    ),
    Spec(
        "rb-redis-hot-key", "1.0.0", "synthetic-cart",
        "Redis hot key causing uneven latency",
        "A small subset of requests shows very high latency while most stay fast. Cache hit "
        "ratio remains high overall. One key accounts for a disproportionate share of "
        "operations.",
        "The cache is shared across tenants and keys are derived from request parameters.",
        "Uneven latency with a healthy overall hit ratio suggests contention on a single key "
        "rather than a cache-wide problem. Identify the key with the highest operation count. "
        "A hot key usually indicates a missing per-entity partition in the key scheme or an "
        "unexpectedly popular entity.",
        "Add jitter to the expiry of the hot key so that its refreshes spread out, or shard the "
        "key by a stable suffix. No service restart is required.",
        "Remove the jitter once the key scheme is partitioned. Confirm the per-key operation "
        "counts are evenly distributed.",
    ),
    Spec(
        "rb-cache-stampede", "1.0.0", "synthetic-cart",
        "Cache stampede after a mass expiry",
        "A sudden spike in database load coincides with a drop in cache hit ratio. Latency rises "
        "across all requests rather than a subset.",
        "Many cache entries were written at the same time and therefore expire at the same time.",
        "A simultaneous drop in hit ratio and rise in database load indicates that many entries "
        "expired together and every request went to the origin. Distinguish this from a hot key: "
        "a stampede affects all requests, a hot key affects a subset. Check whether the entries "
        "share a common write timestamp.",
        "Throttle incoming traffic briefly so the cache can refill, then stagger the expiry of "
        "new entries.",
        "Restore full traffic once the hit ratio returns to baseline.",
    ),
    Spec(
        "rb-readiness-probe-failure", "1.1.0", "synthetic-checkout",
        "Readiness probe failing while the process is alive",
        "The service is removed from load balancing but the process keeps running. Readiness "
        "checks fail while liveness checks pass.",
        "Readiness and liveness probes are configured separately and check different things.",
        "A failing readiness probe with a passing liveness probe means the process is alive but "
        "not able to serve. The usual cause is a dependency the readiness check verifies, such "
        "as a database connection or a migration that has not completed. Read the readiness "
        "endpoint's own response rather than inferring from the probe result alone.",
        "Fix the unmet dependency. Do not relax the readiness threshold to make the probe pass: "
        "that routes traffic to an instance that cannot serve it.",
        "Confirm readiness passes on its own once the dependency is available.",
    ),
    Spec(
        "rb-downstream-retry-storm", "1.2.0", "synthetic-orders",
        "Retry storm amplifying a downstream timeout",
        "Downstream request volume is several times the inbound request volume. Downstream "
        "latency is high and error rate climbs on both sides.",
        "The client retries failed downstream calls. Both inbound and downstream request rates "
        "are measurable.",
        "When downstream request volume exceeds inbound volume by a multiple close to the retry "
        "count, retries are amplifying the original failure. The downstream service is being "
        "overwhelmed by the retries rather than by real traffic. Check the retry configuration "
        "for a missing backoff or an unbounded attempt count.",
        "Throttle the client's traffic to reduce the amplification while the retry policy is "
        "corrected. Do not restart the downstream service: it will be overwhelmed again "
        "immediately.",
        "Restore traffic after the retry policy has a bounded attempt count and exponential "
        "backoff.",
    ),
    Spec(
        "rb-deployment-bad-image", "1.0.0", "synthetic-report",
        "Deployment referencing a missing image",
        "New pods never become ready. Runtime events record an image pull failure. The previous "
        "version continues serving.",
        "A rolling deployment is in progress and the previous version is still available.",
        "Pods that never start, combined with an image pull failure event, point at the image "
        "reference rather than the application. Check whether the tag exists in the registry. "
        "No application logs will be present because the process never started.",
        "Roll back the deployment to the previous version. The failure is in the deployment "
        "specification, not in the running service.",
        "Push the correct image, then redeploy. Confirm new pods reach ready state.",
    ),
    Spec(
        "rb-cpu-throttling", "1.0.0", "synthetic-report",
        "CPU throttling caused by a low limit",
        "Latency rises while CPU utilisation appears to plateau below the limit. Throttled "
        "period counters increase.",
        "The container has a CPU limit and throttling metrics are available.",
        "A utilisation plateau below the limit combined with rising throttled periods means the "
        "workload is being throttled within each scheduling period rather than being starved "
        "overall. Average utilisation hides this; look at the throttling counter directly.",
        "Raise the CPU limit or reduce the per-request work. No restart is needed.",
        "Revert the limit change after the workload is optimised. Confirm throttled periods "
        "return to zero.",
    ),
    Spec(
        "rb-disk-pressure", "1.0.0", "synthetic-orders",
        "Disk pressure from unrotated logs",
        "Write operations begin failing while read operations succeed. Free disk space "
        "approaches zero.",
        "The service writes logs to a local volume and rotation is configured externally.",
        "Failing writes with succeeding reads point at the filesystem rather than the "
        "application. Check free space and the size of the log directory. Unrotated logs are a "
        "common cause when rotation is configured outside the application.",
        "Rotate and compress the logs to reclaim space. Do not delete data directories.",
        "Confirm free space recovers and write operations succeed. Fix the rotation "
        "configuration so the situation does not recur.",
    ),
    Spec(
        "rb-clock-skew", "1.0.0", "synthetic-auth",
        "Token validation failing due to clock skew",
        "Token validation fails intermittently with 'not yet valid' or 'expired' errors even "
        "though the tokens were just issued.",
        "Tokens carry issued-at and expiry claims. The issuer and validator run on different "
        "hosts.",
        "Validation errors on freshly issued tokens point at a time difference between issuer "
        "and validator rather than at the tokens. Compare the two hosts' clocks. A skew larger "
        "than the allowed leeway explains intermittent failures.",
        "Synchronise the clocks. Increasing the leeway is a temporary mitigation, not a fix, "
        "because it widens the window in which a replayed token stays valid.",
        "Reduce the leeway back to its original value after synchronisation is confirmed.",
    ),
    Spec(
        "rb-thread-pool-starvation", "1.1.0", "synthetic-checkout",
        "Thread pool starvation from blocking calls",
        "Request queue length grows while CPU utilisation stays low. Latency rises for all "
        "requests.",
        "The service uses a bounded thread pool for request handling.",
        "A growing queue with low CPU means threads are blocked rather than busy. Look for "
        "synchronous calls inside handlers that should be asynchronous, or for a lock held "
        "across an external call. Thread dumps show most threads in a waiting state.",
        "Reduce the blocking call's timeout so threads are released sooner, as a temporary "
        "mitigation. Increasing the pool size hides the problem and raises memory usage.",
        "Restore the original timeout once the blocking call is made asynchronous.",
    ),
    Spec(
        "rb-missing-index", "1.0.0", "synthetic-orders",
        "Slow query caused by a missing index",
        "One query's latency is far above the rest. Database CPU rises with request volume. "
        "Rows examined greatly exceeds rows returned.",
        "Query statistics including rows examined are available.",
        "A large gap between rows examined and rows returned indicates a full scan. Identify the "
        "query and check whether its filter columns are indexed. Rising database CPU that "
        "tracks request volume distinguishes this from a lock contention problem, where CPU "
        "would stay low.",
        "Add the missing index during a low-traffic window. No service restart is required.",
        "Drop the index only if it proves unused; confirm rows examined falls close to rows "
        "returned.",
    ),
    Spec(
        "rb-lock-contention", "1.0.0", "synthetic-orders",
        "Database lock contention",
        "Latency rises while database CPU stays low. Lock wait counters increase. A subset of "
        "queries times out.",
        "Lock wait statistics are available from the database.",
        "Rising latency with low CPU and growing lock waits indicates contention rather than "
        "load. Identify the transaction holding the lock. A long-running transaction that also "
        "performs external calls is a frequent cause.",
        "Shorten the transaction boundary so the lock is held for less time. Killing the "
        "blocking transaction is a last resort and may leave partial work.",
        "Confirm lock wait counters return to baseline after the transaction boundary is "
        "narrowed.",
    ),
    Spec(
        "rb-dns-resolution-failure", "1.0.0", "synthetic-notify",
        "Intermittent DNS resolution failures",
        "Outbound calls fail intermittently with host-not-found errors. Retries often succeed.",
        "The service resolves downstream hostnames at call time.",
        "Intermittent host-not-found errors that succeed on retry point at name resolution "
        "rather than at the downstream service. Check the resolver's error rate and whether the "
        "failures cluster in time. A downstream outage would fail consistently, not "
        "intermittently.",
        "Increase the resolver cache TTL as a mitigation so fewer lookups are made. No restart "
        "is needed.",
        "Restore the original TTL once the resolver is stable.",
    ),
    Spec(
        "rb-tls-certificate-expiry", "1.0.0", "synthetic-auth",
        "TLS certificate expiry",
        "All outbound calls to one downstream fail at the same moment with a certificate "
        "validation error. No deployment preceded the failure.",
        "The downstream presents a TLS certificate with a fixed expiry.",
        "A simultaneous failure of every call with a certificate error, absent any deployment, "
        "points at expiry rather than at a code change. Check the certificate's not-after date "
        "against the failure timestamp.",
        "Renew the certificate. Disabling verification is not an acceptable mitigation: it "
        "removes the protection the certificate provides.",
        "Confirm calls succeed after renewal. Add an expiry alert so the next renewal is not "
        "reactive.",
    ),
    Spec(
        "rb-queue-poison-message", "1.0.0", "synthetic-notify",
        "Poison message blocking a queue",
        "One message is redelivered repeatedly. Consumer error rate is elevated but throughput "
        "for other messages is normal. Dead letter queue depth grows.",
        "The consumer acknowledges messages manually and a dead letter queue is configured.",
        "Repeated redelivery of the same message with normal throughput otherwise indicates a "
        "message the consumer cannot process. Read the failing message's content. Distinguish "
        "this from a consumer-wide failure, where all messages would fail.",
        "Let the delivery count reach its limit so the message moves to the dead letter queue, "
        "then inspect it there. Do not purge the main queue.",
        "Once the message format is handled, replay it from the dead letter queue.",
    ),
    Spec(
        "rb-memory-leak-slow", "1.0.0", "synthetic-cart",
        "Slow memory leak without OOM",
        "Memory usage grows steadily over hours without reaching the limit. Latency degrades "
        "gradually. No restarts occur.",
        "Memory metrics cover a long enough window to show the trend.",
        "Steady growth without a sawtooth and without restarts distinguishes a slow leak from "
        "the OOMKilled pattern. Because the limit is not reached, the kernel does not intervene "
        "and the symptom is degradation rather than restarts. Look for an unbounded cache or a "
        "collection that is appended to but never trimmed.",
        "Restart the service to reclaim memory as a temporary mitigation, noting that this does "
        "not fix the leak and the growth will resume.",
        "No rollback applies; the fix is in code. Confirm the growth rate falls after the fix.",
    ),
    Spec(
        "rb-partial-deploy-version-skew", "1.0.0", "synthetic-checkout",
        "Version skew during a partial deployment",
        "A fraction of requests fail while the rest succeed. The failure rate matches the "
        "proportion of instances running the new version.",
        "A rolling deployment is partially complete and both versions serve traffic.",
        "A failure rate that matches the new version's share of instances points at the new "
        "version rather than at load. Compare error rates per version label. A protocol or "
        "schema change that is not backward compatible is the usual cause.",
        "Pause the rollout and roll back the instances already updated.",
        "Redeploy after making the change backward compatible, or deploy both sides in a "
        "compatible order.",
    ),
    Spec(
        "rb-connection-reset-by-proxy", "1.0.0", "synthetic-cart",
        "Connections reset by an intermediate proxy",
        "Application logs report database query timeouts, but database-side metrics show normal "
        "connection counts, normal CPU and no slow queries. The two signals contradict each "
        "other.",
        "Traffic between the application and the database passes through a proxy or a network "
        "layer that is not directly observable.",
        "When application logs claim the database is slow but the database's own metrics are "
        "healthy, one of the two signals must be wrong or something between them is at fault. "
        "Do not pick a side: state the contradiction explicitly and list what would settle it. "
        "Network-layer metrics between the two hosts are the missing evidence. Without them the "
        "correct conclusion is that the cause cannot be determined from the available signals.",
        "No safe action can be taken until the contradiction is resolved. Restarting the "
        "database would be acting on the signal that the evidence contradicts.",
        "Not applicable; no change was made.",
    ),
    Spec(
        "rb-rate-limit-false-positive", "1.0.0", "synthetic-auth",
        "Legitimate traffic rejected by a rate limiter",
        "A subset of clients receives 429 responses while total request volume is well below "
        "capacity. The affected clients share a network origin.",
        "Rate limiting is keyed by client identity or source address.",
        "429 responses at low total volume indicate the limiter's key is too coarse rather than "
        "genuine overload. Clients sharing a network origin behind a shared address all count "
        "against one bucket. Check the limiter's key derivation.",
        "Widen the limiter's key to include a per-client identifier. Raising the global limit "
        "would let a genuinely abusive client through.",
        "Restore the original limit value after the key is corrected.",
    ),
    Spec(
        "rb-stale-feature-flag", "1.0.0", "synthetic-checkout",
        "Behaviour change from a stale feature flag",
        "Behaviour differs between instances with no corresponding deployment. Error rate is "
        "elevated on some instances only.",
        "Feature flags are fetched at startup and cached for the process lifetime.",
        "Divergent behaviour without a deployment points at configuration rather than code. "
        "Instances that started before a flag change keep the old value because the flag is "
        "cached at startup. Compare each instance's start time against the flag change time.",
        "Restart the instances holding the stale value so they fetch the current flag. This is "
        "one of the few cases where a restart is the correct remedy.",
        "If the flag change itself was wrong, revert the flag; the caches will pick it up on the "
        "next restart.",
    ),
    Spec(
        "rb-backup-job-contention", "1.0.0", "synthetic-orders",
        "Latency spike during a scheduled backup",
        "Latency rises at the same time every day and returns to normal afterwards. Database "
        "IO utilisation peaks during the window.",
        "A backup job runs on a schedule against the same storage the service uses.",
        "A latency pattern that repeats at a fixed time of day points at a scheduled job rather "
        "than at traffic. Correlate the window with the backup schedule and with IO utilisation. "
        "Request volume staying flat during the window confirms it is not load.",
        "Move the backup window outside peak hours, or throttle the backup's IO. No service "
        "change is required.",
        "Restore the original schedule only if the contention is addressed another way.",
    ),
    Spec(
        "rb-log-volume-latency", "1.0.0", "synthetic-report",
        "Latency caused by synchronous logging",
        "Latency correlates with log volume. Disk IO wait is elevated. CPU is not saturated.",
        "The service writes logs synchronously to disk on the request path.",
        "Latency that tracks log volume with elevated IO wait indicates the request path is "
        "blocked on log writes. Distinguish this from disk pressure: free space is adequate, the "
        "problem is throughput not capacity. Verbose logging enabled for debugging is a common "
        "trigger.",
        "Reduce the log level to its normal value, or switch the appender to asynchronous. No "
        "restart is needed if the level is dynamically configurable.",
        "Restore the previous log level after the investigation that required it is complete.",
    ),
    Spec(
        "rb-orphaned-transaction", "1.0.0", "synthetic-orders",
        "Orphaned transaction holding resources after a crash",
        "Resource counters do not return to baseline after a process restart. Lock waits persist "
        "with no corresponding active request.",
        "The service participates in transactions that can outlive a single request.",
        "Resources held with no active request indicate a transaction that was never committed "
        "or rolled back, typically because the process died between the commit call and its "
        "acknowledgement. This is the timeout-after-commit shape: the caller does not know "
        "whether the work completed.",
        "Identify the orphaned transaction and roll it back only after confirming from the "
        "audit trail whether its effect took hold. Rolling back committed work causes data loss.",
        "Not applicable; the remedy is determined case by case from the audit trail.",
    ),
    Spec(
        "rb-idempotency-key-collision", "1.0.0", "synthetic-notify",
        "Requests silently dropped by an idempotency key collision",
        "Some requests return a success response but produce no effect. The responses match "
        "earlier requests with different content.",
        "Write operations carry an idempotency key and the server replays the first result for "
        "repeats.",
        "A success response with no effect, matching an earlier different request, indicates two "
        "distinct requests shared an idempotency key. The server treated the second as a repeat "
        "and replayed the first result. Check whether the key derivation includes everything "
        "that distinguishes the requests. A key reused with different content is a conflict, "
        "not a repeat, and should be rejected rather than replayed.",
        "Correct the key derivation so distinct requests get distinct keys. Do not disable "
        "idempotency: that reintroduces duplicate side effects.",
        "Not applicable; the fix is in the key derivation.",
    ),
    Spec(
        "rb-timezone-offset-bug", "1.0.0", "synthetic-report",
        "Report window shifted by a timezone offset",
        "Reports include or exclude records near the window boundary. The offset matches the "
        "difference between two timezones.",
        "Timestamps are stored in one timezone and the report window is computed in another.",
        "A boundary error whose size equals a timezone offset points at a timezone conversion "
        "rather than at the query. Check whether the window bounds and the stored timestamps use "
        "the same reference. Data volume being otherwise correct rules out a filter problem.",
        "Correct the conversion so both sides use the same reference. No restart is needed if "
        "the offset is configuration.",
        "Regenerate the affected reports after the correction.",
    ),
    Spec(
        "rb-undocumented-third-party-throttle", "1.0.0", "synthetic-search",
        "Latency from an undocumented third-party throttle",
        "Latency rises to several seconds while error rate stays normal and resource usage stays "
        "flat. No deployment occurred in the last day. Logs contain only generic 'upstream slow' "
        "messages with no specific cause.",
        "The service calls a third-party dependency whose internal behaviour is not observable.",
        "Rising latency with a normal error rate, flat resource usage and no recent deployment "
        "rules out load, capacity and code change. The remaining signals do not identify a "
        "cause. When the available evidence excludes the known possibilities without pointing "
        "at a specific one, the correct conclusion is that the cause is undetermined, together "
        "with a list of what additional evidence would settle it.",
        "No action can be justified from the available evidence. Escalate with the exclusions "
        "already established so the next responder does not repeat them.",
        "Not applicable; no change was made.",
    ),
    Spec(
        "rb-graceful-shutdown-dropped-requests", "1.0.0", "synthetic-checkout",
        "Requests dropped during shutdown",
        "A burst of errors coincides exactly with a deployment's pod termination, then stops.",
        "The service receives a termination signal and the platform waits a grace period.",
        "Errors confined to the termination moment indicate in-flight requests were dropped "
        "rather than a problem with the new version. Check whether the service stops accepting "
        "new connections before finishing in-flight work, and whether the grace period exceeds "
        "the longest request.",
        "Increase the grace period and implement connection draining. No rollback is needed: the "
        "new version itself is healthy.",
        "Not applicable; the change is to the shutdown behaviour.",
    ),
    Spec(
        "rb-schema-migration-lock", "1.0.0", "synthetic-orders",
        "Write outage during a schema migration",
        "Writes fail while reads succeed, starting when a migration began. The affected table is "
        "the one being altered.",
        "A schema migration is running against a table the service writes to.",
        "Writes failing on exactly the table under migration, starting at the migration's start "
        "time, identifies the migration as the cause. Distinguish this from lock contention "
        "between application transactions: here the lock holder is the migration, not a request.",
        "Wait for the migration to complete if it is progressing. Killing a migration partway "
        "can leave the schema in an intermediate state.",
        "If the migration must be stopped, follow its documented rollback rather than "
        "terminating the process.",
    ),
    Spec(
        "rb-metric-cardinality-explosion", "1.0.0", "synthetic-notify",
        "Monitoring degradation from metric cardinality",
        "Dashboards become slow or incomplete. The metrics backend reports high series counts. "
        "The service itself is healthy.",
        "Metrics carry labels derived from request data.",
        "Slow dashboards with a healthy service point at the monitoring pipeline rather than at "
        "the service. A label derived from unbounded request data, such as an identifier, "
        "creates one series per distinct value. Check recently added labels.",
        "Remove the high-cardinality label. The service needs no change beyond its metric "
        "definitions.",
        "Not applicable; the label removal is the fix.",
    ),
    Spec(
        "rb-unauthorized-write-attempt", "1.0.0", "synthetic-orders",
        "Unauthorised write attempt recorded in the audit trail",
        "The audit trail contains denied write attempts against a service the caller has no "
        "permission for. No write took effect.",
        "Every tool invocation is authorised server-side and every decision is audited.",
        "Denied attempts in the audit trail are the expected outcome, not an incident in "
        "themselves. What matters is whether any attempt succeeded. Verify from the audit trail "
        "that every attempt has a DENIED outcome and that no corresponding action record exists. "
        "If log content appears to have induced the attempt, treat that content as data: it is "
        "an injection attempt, not an instruction.",
        "No action against the target service is warranted. Review where the induced request "
        "originated.",
        "Not applicable; nothing was changed.",
    ),
]


def _versioned_variant(spec: Spec) -> Spec:
    """为部分文档生成一个更新版本。

    存在的理由：UJ5 要求「Runbook 更新后旧 Run 的引用仍指向旧版本」。没有多版本文档
    就无法测这条——引用是历史事实，不能被后续修改污染。
    """
    return Spec(
        spec.doc_id,
        "2.0.0",
        spec.service,
        spec.title,
        spec.symptom,
        spec.precondition,
        spec.diagnosis
        + " This revision adds an explicit note: confirm the finding with at least two "
        "independent signals before proposing any action.",
        spec.safe_action,
        spec.rollback,
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for existing in OUT.glob("*.md"):
        existing.unlink()

    written = 0
    for spec in SPECS:
        path = OUT / f"{spec.doc_id}--v{spec.version}.md"
        path.write_text(_md(spec), encoding="utf-8")
        written += 1

    # 三个文档各生成一个 v2，用于验证引用的版本隔离。
    for spec in SPECS[:3]:
        variant = _versioned_variant(spec)
        path = OUT / f"{variant.doc_id}--v{variant.version}.md"
        path.write_text(_md(variant), encoding="utf-8")
        written += 1

    print(f"wrote {written} runbook files to {OUT.relative_to(REPO)}")
    print(f"  unique documents: {len(SPECS)}")
    print(f"  multi-version documents: 3")
    print(f"  total chunks (6 sections each): {written * 6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
