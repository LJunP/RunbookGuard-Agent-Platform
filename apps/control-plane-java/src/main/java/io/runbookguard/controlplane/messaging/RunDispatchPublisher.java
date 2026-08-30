package io.runbookguard.controlplane.messaging;

import io.runbookguard.controlplane.domain.AgentRun;
import io.runbookguard.controlplane.persistence.AgentRunMapper;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import io.runbookguard.controlplane.audit.AuditService;

@Service
public class RunDispatchPublisher {

    private final RabbitTemplate rabbit;
    private final AgentRunMapper runMapper;
    private final AuditService audit;

    public RunDispatchPublisher(RabbitTemplate rabbit, AgentRunMapper runMapper, AuditService audit) {
        this.rabbit = rabbit;
        this.runMapper = runMapper;
        this.audit = audit;
    }

    /**
     * dispatch_sequence 自增后持久化，再据此构造幂等键。这样生产者重启后重发同一个 Run，
     * 用的仍是数据库里那个序号——键可重现是幂等生效的前提（ADR-0003 §4）。
     */
    @Transactional
    public RunMessages.RunDispatch dispatch(AgentRun run, String resumeFromCheckpointId) {
        runMapper.bumpDispatchSequence(run.runId());
        int sequence = runMapper.currentDispatchSequence(run.runId());
        RunMessages.RunDispatch message = new RunMessages.RunDispatch(
                RunMessages.SCHEMA_VERSION, run.runId(), run.incidentId(), run.tenantId(),
                run.principalId(), sequence, run.graphVersion(), run.promptVersion(),
                run.modelId(), run.datasetVersion(), run.maxSteps(), run.deadline(),
                run.costBudgetMicros(), run.tokenBudget(), run.toolCallBudget(),
                resumeFromCheckpointId);

        rabbit.convertAndSend(RabbitTopologyConfig.EXCHANGE, RabbitTopologyConfig.ROUTING_DISPATCH,
                message, m -> {
                    var props = m.getMessageProperties();
                    props.setHeader("x-idempotency-key", message.idempotencyKey());
                    props.setHeader("x-delivery-count", 1);
                    props.setHeader("x-tenant-id", run.tenantId());
                    props.setHeader("x-schema-version", RunMessages.SCHEMA_VERSION);
                    props.setMessageId(message.idempotencyKey());
                    return m;
                });

        audit.allowed(run.tenantId(), run.principalId(), "run.dispatch", "run", run.runId(),
                "{\"sequence\":%d,\"resumeFrom\":%s}".formatted(sequence,
                        resumeFromCheckpointId == null ? "null" : "\"" + resumeFromCheckpointId + "\""));
        return message;
    }
}
