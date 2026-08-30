package io.runbookguard.controlplane.messaging;

import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.BindingBuilder;
import org.springframework.amqp.core.DirectExchange;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.core.QueueBuilder;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.amqp.support.converter.Jackson2JsonMessageConverter;
import org.springframework.amqp.support.converter.MessageConverter;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import com.fasterxml.jackson.databind.ObjectMapper;

/** 拓扑定义见 contracts/events/README.md。 */
@Configuration
public class RabbitTopologyConfig {

    public static final String EXCHANGE = "rg.run";
    public static final String DLX = "rg.run.dlx";

    public static final String ROUTING_DISPATCH = "run.dispatch";
    public static final String ROUTING_PROGRESS = "run.progress";

    public static final String QUEUE_DISPATCH = "rg.run.dispatch";
    public static final String QUEUE_PROGRESS = "rg.run.progress";
    public static final String QUEUE_DISPATCH_DLQ = "rg.run.dispatch.dlq";
    public static final String QUEUE_PROGRESS_DLQ = "rg.run.progress.dlq";

    @Bean
    public DirectExchange runExchange() {
        return new DirectExchange(EXCHANGE, true, false);
    }

    @Bean
    public DirectExchange runDeadLetterExchange() {
        return new DirectExchange(DLX, true, false);
    }

    /**
     * DLQ 只作事后取证，不是恢复路径。恢复路径是「投递计数达上限 → 主动结算终态」，
     * 因为悬挂在 DLQ 里的 Run 既不成功也不失败，M6 的恢复唯一终态率就无法计算。
     */
    @Bean
    public Queue dispatchQueue() {
        return QueueBuilder.durable(QUEUE_DISPATCH)
                .deadLetterExchange(DLX)
                .deadLetterRoutingKey(ROUTING_DISPATCH)
                .build();
    }

    @Bean
    public Queue progressQueue() {
        return QueueBuilder.durable(QUEUE_PROGRESS)
                .deadLetterExchange(DLX)
                .deadLetterRoutingKey(ROUTING_PROGRESS)
                .build();
    }

    @Bean
    public Queue dispatchDlq() {
        return QueueBuilder.durable(QUEUE_DISPATCH_DLQ).build();
    }

    @Bean
    public Queue progressDlq() {
        return QueueBuilder.durable(QUEUE_PROGRESS_DLQ).build();
    }

    @Bean
    public Binding dispatchBinding() {
        return BindingBuilder.bind(dispatchQueue()).to(runExchange()).with(ROUTING_DISPATCH);
    }

    @Bean
    public Binding progressBinding() {
        return BindingBuilder.bind(progressQueue()).to(runExchange()).with(ROUTING_PROGRESS);
    }

    @Bean
    public Binding dispatchDlqBinding() {
        return BindingBuilder.bind(dispatchDlq()).to(runDeadLetterExchange()).with(ROUTING_DISPATCH);
    }

    @Bean
    public Binding progressDlqBinding() {
        return BindingBuilder.bind(progressDlq()).to(runDeadLetterExchange()).with(ROUTING_PROGRESS);
    }

    @Bean
    public MessageConverter jsonMessageConverter(ObjectMapper objectMapper) {
        return new Jackson2JsonMessageConverter(objectMapper);
    }

    @Bean
    public RabbitTemplate rabbitTemplate(ConnectionFactory connectionFactory,
                                         MessageConverter converter) {
        RabbitTemplate template = new RabbitTemplate(connectionFactory);
        template.setMessageConverter(converter);
        // publisher confirm 不在 M2 范围（需要额外的回调与重发逻辑）。
        // 当前保证是：Control Plane 在同一事务提交后才发消息，消息丢失时由 Run 的
        // 超时扫描重新派发。这一点在 M2 Gate 报告中作为已知限制记录。
        return template;
    }
}
