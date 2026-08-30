"""检索评测查询集（M5）。

人工标注的相关段落。它与语料一起冻结——M6 冻结 dataset 时冻结的包括这份标注，
否则 Recall 的分母会变，而"指标提升了"就说不清是检索变好还是标注变松。

标注原则：只标**直接回答查询**的段落。一个查询问"这是什么故障"，就只标 symptom 与
diagnosis；不把整个文档的六段都标上——那会让 Recall 虚高，因为检索器随便召回同一文档的
任何段落都算命中。
"""

from __future__ import annotations

from .metrics import RetrievalQuery


def _q(
    query_id: str,
    text: str,
    service: str | None,
    relevant: list[str],
    *,
    expects_miss: bool = False,
) -> RetrievalQuery:
    return RetrievalQuery(
        query_id=query_id,
        text=text,
        service=service,
        relevant_section_ids=frozenset(relevant),
        expects_miss=expects_miss,
    )


# 诊断类查询：问「这是什么故障」。
DIAGNOSTIC_QUERIES: list[RetrievalQuery] = [
    _q(
        "d-pool-saturation",
        "db_pool_active reached db_pool_max and requests time out after 5000ms",
        "synthetic-orders",
        ["rb-db-pool-exhaustion#symptom", "rb-db-pool-exhaustion#diagnosis"],
    ),
    _q(
        "d-queue-depth",
        "queue depth keeps growing and the oldest message is 900 seconds old",
        "synthetic-notify",
        ["rb-mq-consumer-backlog#symptom", "rb-mq-consumer-backlog#diagnosis"],
    ),
    _q(
        "d-audience-401",
        "401 from the downstream auth service with audience mismatch right after a deploy",
        "synthetic-checkout",
        ["rb-config-audience-mismatch#symptom", "rb-config-audience-mismatch#diagnosis"],
    ),
    _q(
        "d-oomkilled",
        "container restarts repeatedly with exit code 137 and memory reaches the limit",
        "synthetic-report",
        ["rb-container-oomkilled#symptom", "rb-container-oomkilled#diagnosis"],
    ),
    _q(
        "d-hot-key",
        "a few requests are very slow while cache hit ratio stays high",
        "synthetic-cart",
        ["rb-redis-hot-key#symptom", "rb-redis-hot-key#diagnosis"],
    ),
    _q(
        "d-stampede",
        "cache hit ratio dropped and database load spiked at the same moment",
        "synthetic-cart",
        ["rb-cache-stampede#symptom", "rb-cache-stampede#diagnosis"],
    ),
    _q(
        "d-readiness",
        "readiness probe fails but liveness passes and the process is still running",
        "synthetic-checkout",
        ["rb-readiness-probe-failure#symptom", "rb-readiness-probe-failure#diagnosis"],
    ),
    _q(
        "d-retry-storm",
        "downstream request volume is several times the inbound volume",
        "synthetic-orders",
        ["rb-downstream-retry-storm#symptom", "rb-downstream-retry-storm#diagnosis"],
    ),
    _q(
        "d-bad-image",
        "new pods never become ready and there is an image pull failure event",
        "synthetic-report",
        ["rb-deployment-bad-image#symptom", "rb-deployment-bad-image#diagnosis"],
    ),
    _q(
        "d-cpu-throttle",
        "latency rises while cpu utilisation plateaus below the limit and throttled periods grow",
        "synthetic-report",
        ["rb-cpu-throttling#symptom", "rb-cpu-throttling#diagnosis"],
    ),
    _q(
        "d-disk-pressure",
        "write operations fail while reads succeed and free disk space is near zero",
        "synthetic-orders",
        ["rb-disk-pressure#symptom", "rb-disk-pressure#diagnosis"],
    ),
    _q(
        "d-clock-skew",
        "token validation fails with not yet valid on freshly issued tokens",
        "synthetic-auth",
        ["rb-clock-skew#symptom", "rb-clock-skew#diagnosis"],
    ),
    _q(
        "d-thread-starvation",
        "request queue grows while cpu utilisation stays low",
        "synthetic-checkout",
        ["rb-thread-pool-starvation#symptom", "rb-thread-pool-starvation#diagnosis"],
    ),
    _q(
        "d-missing-index",
        "rows examined greatly exceeds rows returned for one query",
        "synthetic-orders",
        ["rb-missing-index#symptom", "rb-missing-index#diagnosis"],
    ),
    _q(
        "d-lock-contention",
        "latency rises while database cpu stays low and lock waits increase",
        "synthetic-orders",
        ["rb-lock-contention#symptom", "rb-lock-contention#diagnosis"],
    ),
    _q(
        "d-dns",
        "outbound calls fail intermittently with host not found and retries succeed",
        "synthetic-notify",
        ["rb-dns-resolution-failure#symptom", "rb-dns-resolution-failure#diagnosis"],
    ),
    _q(
        "d-tls-expiry",
        "every call to one downstream fails at the same moment with a certificate error",
        "synthetic-auth",
        ["rb-tls-certificate-expiry#symptom", "rb-tls-certificate-expiry#diagnosis"],
    ),
    _q(
        "d-poison-message",
        "one message is redelivered repeatedly while other messages process normally",
        "synthetic-notify",
        ["rb-queue-poison-message#symptom", "rb-queue-poison-message#diagnosis"],
    ),
    _q(
        "d-slow-leak",
        "memory grows steadily over hours without reaching the limit and no restarts happen",
        "synthetic-cart",
        ["rb-memory-leak-slow#symptom", "rb-memory-leak-slow#diagnosis"],
    ),
    _q(
        "d-version-skew",
        "a fraction of requests fail matching the share of instances on the new version",
        "synthetic-checkout",
        [
            "rb-partial-deploy-version-skew#symptom",
            "rb-partial-deploy-version-skew#diagnosis",
        ],
    ),
    _q(
        "d-contradiction",
        "application logs report database timeouts but database metrics are all normal",
        "synthetic-cart",
        [
            "rb-connection-reset-by-proxy#symptom",
            "rb-connection-reset-by-proxy#diagnosis",
        ],
    ),
    _q(
        "d-rate-limit",
        "clients get 429 responses while total volume is well below capacity",
        "synthetic-auth",
        ["rb-rate-limit-false-positive#symptom", "rb-rate-limit-false-positive#diagnosis"],
    ),
    _q(
        "d-stale-flag",
        "behaviour differs between instances with no deployment",
        "synthetic-checkout",
        ["rb-stale-feature-flag#symptom", "rb-stale-feature-flag#diagnosis"],
    ),
    _q(
        "d-backup-window",
        "latency rises at the same time every day and returns to normal afterwards",
        "synthetic-orders",
        ["rb-backup-job-contention#symptom", "rb-backup-job-contention#diagnosis"],
    ),
    _q(
        "d-sync-logging",
        "latency correlates with log volume and io wait is elevated",
        "synthetic-report",
        ["rb-log-volume-latency#symptom", "rb-log-volume-latency#diagnosis"],
    ),
    _q(
        "d-orphaned-transaction",
        "resource counters do not return to baseline after a restart and lock waits persist",
        "synthetic-orders",
        ["rb-orphaned-transaction#symptom", "rb-orphaned-transaction#diagnosis"],
    ),
    _q(
        "d-idempotency-collision",
        "some requests return success but produce no effect",
        "synthetic-notify",
        [
            "rb-idempotency-key-collision#symptom",
            "rb-idempotency-key-collision#diagnosis",
        ],
    ),
    _q(
        "d-timezone",
        "reports include or exclude records near the window boundary",
        "synthetic-report",
        ["rb-timezone-offset-bug#symptom", "rb-timezone-offset-bug#diagnosis"],
    ),
    _q(
        "d-undetermined-latency",
        "latency rises to several seconds with normal error rate flat resources and no deploy",
        "synthetic-search",
        [
            "rb-undocumented-third-party-throttle#symptom",
            "rb-undocumented-third-party-throttle#diagnosis",
        ],
    ),
    _q(
        "d-shutdown-drops",
        "a burst of errors coincides exactly with pod termination then stops",
        "synthetic-checkout",
        [
            "rb-graceful-shutdown-dropped-requests#symptom",
            "rb-graceful-shutdown-dropped-requests#diagnosis",
        ],
    ),
    _q(
        "d-migration-lock",
        "writes fail while reads succeed starting when a migration began",
        "synthetic-orders",
        ["rb-schema-migration-lock#symptom", "rb-schema-migration-lock#diagnosis"],
    ),
    _q(
        "d-cardinality",
        "dashboards became slow and the metrics backend reports high series counts",
        "synthetic-notify",
        [
            "rb-metric-cardinality-explosion#symptom",
            "rb-metric-cardinality-explosion#diagnosis",
        ],
    ),
    _q(
        "d-denied-write",
        "the audit trail contains denied write attempts and no write took effect",
        "synthetic-orders",
        [
            "rb-unauthorized-write-attempt#symptom",
            "rb-unauthorized-write-attempt#diagnosis",
        ],
    ),
]

# 处置类查询：问「怎么办」。它们的相关段落是 safe_action / rollback，
# 用于验证 rerank 的段落类型先验真的起作用。
REMEDIAL_QUERIES: list[RetrievalQuery] = [
    _q(
        "r-pool-fix",
        "how do I remediate a saturated connection pool after a release",
        "synthetic-orders",
        ["rb-db-pool-exhaustion#safe_action", "rb-db-pool-exhaustion#rollback"],
    ),
    _q(
        "r-queue-fix",
        "what action should I take to mitigate a growing queue backlog",
        "synthetic-notify",
        ["rb-mq-consumer-backlog#safe_action", "rb-mq-consumer-backlog#rollback"],
    ),
    _q(
        "r-audience-fix",
        "how do I rollback a wrong audience configuration",
        "synthetic-checkout",
        [
            "rb-config-audience-mismatch#safe_action",
            "rb-config-audience-mismatch#rollback",
        ],
    ),
    _q(
        "r-oom-fix",
        "what is the safe action for a container that keeps getting oomkilled",
        "synthetic-report",
        ["rb-container-oomkilled#safe_action", "rb-container-oomkilled#rollback"],
    ),
    _q(
        "r-throttle-fix",
        "how do I mitigate a retry storm hitting a downstream service",
        "synthetic-orders",
        [
            "rb-downstream-retry-storm#safe_action",
            "rb-downstream-retry-storm#rollback",
        ],
    ),
]

# 期望未命中的查询。S7（Runbook 缺失）的正确弃答依赖它们——
# 一个只测「能找到」的评测集无法证明系统会正确说「没找到」。
MISS_QUERIES: list[RetrievalQuery] = [
    _q(
        "m-quantum-decoherence",
        "quantum decoherence in the flux capacitor manifold",
        None,
        [],
        expects_miss=True,
    ),
    _q(
        "m-payroll",
        "how do I process the monthly payroll run for the finance department",
        None,
        [],
        expects_miss=True,
    ),
    _q(
        "m-gibberish",
        "zzzzz qqqqq wwwww vvvvv",
        None,
        [],
        expects_miss=True,
    ),
    _q(
        "m-kernel-panic",
        "bare metal kernel panic in the storage controller firmware",
        None,
        [],
        expects_miss=True,
    ),
]

ALL_QUERIES: list[RetrievalQuery] = DIAGNOSTIC_QUERIES + REMEDIAL_QUERIES + MISS_QUERIES
