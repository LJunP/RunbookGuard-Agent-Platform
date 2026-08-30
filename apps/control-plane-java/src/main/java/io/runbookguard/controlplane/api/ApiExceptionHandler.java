package io.runbookguard.controlplane.api;

import io.runbookguard.controlplane.approval.NonCanonicalizableArgumentException;
import io.runbookguard.controlplane.security.SecretRedactor;
import io.runbookguard.controlplane.service.ApprovalNotDecidableException;
import io.runbookguard.controlplane.service.ApprovalVerificationFailedException;
import io.runbookguard.controlplane.service.OptimisticLockConflictException;
import io.runbookguard.controlplane.service.TerminalStateAlreadySetException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.time.Instant;
import java.util.Map;

/**
 * 所有出站错误文本都过一遍脱敏（威胁 T-3）。未预期异常一律返回固定文案，把细节留在日志里：
 * 异常消息可能包含 SQL、连接串或请求体片段。
 */
@RestControllerAdvice
public class ApiExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(ApiExceptionHandler.class);

    @ExceptionHandler(UnauthenticatedException.class)
    public ResponseEntity<Map<String, Object>> onUnauthenticated(UnauthenticatedException e) {
        return body(HttpStatus.UNAUTHORIZED, "unauthenticated", e.getMessage());
    }

    @ExceptionHandler(ForbiddenException.class)
    public ResponseEntity<Map<String, Object>> onForbidden(ForbiddenException e) {
        return body(HttpStatus.FORBIDDEN, "forbidden", e.getMessage());
    }

    @ExceptionHandler(ResourceNotFoundException.class)
    public ResponseEntity<Map<String, Object>> onNotFound(ResourceNotFoundException e) {
        return body(HttpStatus.NOT_FOUND, "not_found", e.getMessage());
    }

    @ExceptionHandler(RateLimitExceededException.class)
    public ResponseEntity<Map<String, Object>> onRateLimit(RateLimitExceededException e) {
        return body(HttpStatus.TOO_MANY_REQUESTS, "rate_limited", e.getMessage());
    }

    @ExceptionHandler(OptimisticLockConflictException.class)
    public ResponseEntity<Map<String, Object>> onConflict(OptimisticLockConflictException e) {
        return body(HttpStatus.CONFLICT, "version_conflict", e.getMessage());
    }

    @ExceptionHandler(TerminalStateAlreadySetException.class)
    public ResponseEntity<Map<String, Object>> onTerminal(TerminalStateAlreadySetException e) {
        return body(HttpStatus.CONFLICT, "already_terminal", e.getMessage());
    }

    @ExceptionHandler(ApprovalVerificationFailedException.class)
    public ResponseEntity<Map<String, Object>> onApprovalVerification(
            ApprovalVerificationFailedException e) {
        return body(HttpStatus.FORBIDDEN, "approval_verification_failed", e.getMessage());
    }

    @ExceptionHandler(ApprovalNotDecidableException.class)
    public ResponseEntity<Map<String, Object>> onApprovalNotDecidable(ApprovalNotDecidableException e) {
        return body(HttpStatus.CONFLICT, "approval_not_decidable", e.getMessage());
    }

    @ExceptionHandler(NonCanonicalizableArgumentException.class)
    public ResponseEntity<Map<String, Object>> onNonCanonicalizable(
            NonCanonicalizableArgumentException e) {
        return body(HttpStatus.BAD_REQUEST, "invalid_arguments", e.getMessage());
    }

    @ExceptionHandler(io.runbookguard.controlplane.worker.StaleFencingTokenException.class)
    public ResponseEntity<Map<String, Object>> onStaleFencingToken(
            io.runbookguard.controlplane.worker.StaleFencingTokenException e) {
        return body(HttpStatus.CONFLICT, "stale_fencing_token", e.getMessage());
    }

    @ExceptionHandler(io.runbookguard.controlplane.idempotency.IdempotencyConflictException.class)
    public ResponseEntity<Map<String, Object>> onIdempotencyConflict(
            io.runbookguard.controlplane.idempotency.IdempotencyConflictException e) {
        return body(HttpStatus.CONFLICT, "idempotency_conflict", e.getMessage());
    }

    @ExceptionHandler(io.runbookguard.controlplane.idempotency.IdempotencyInProgressException.class)
    public ResponseEntity<Map<String, Object>> onIdempotencyInProgress(
            io.runbookguard.controlplane.idempotency.IdempotencyInProgressException e) {
        return body(HttpStatus.CONFLICT, "idempotency_in_progress", e.getMessage());
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<Map<String, Object>> onValidation(MethodArgumentNotValidException e) {
        String detail = e.getBindingResult().getFieldErrors().stream()
                .map(fe -> fe.getField() + ": " + fe.getDefaultMessage())
                .reduce((a, b) -> a + "; " + b)
                .orElse("validation failed");
        return body(HttpStatus.BAD_REQUEST, "validation_failed", detail);
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<Map<String, Object>> onIllegalArgument(IllegalArgumentException e) {
        return body(HttpStatus.BAD_REQUEST, "invalid_request", e.getMessage());
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<Map<String, Object>> onUnexpected(Exception e) {
        log.error("unhandled exception", e);
        return body(HttpStatus.INTERNAL_SERVER_ERROR, "internal_error", "unexpected server error");
    }

    private ResponseEntity<Map<String, Object>> body(HttpStatus status, String code, String message) {
        return ResponseEntity.status(status).body(Map.of(
                "error", code,
                "message", SecretRedactor.redact(message == null ? "" : message),
                "timestamp", Instant.now().toString()));
    }
}
