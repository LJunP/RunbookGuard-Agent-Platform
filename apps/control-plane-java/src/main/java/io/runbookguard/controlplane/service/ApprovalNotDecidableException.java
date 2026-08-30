package io.runbookguard.controlplane.service;

public class ApprovalNotDecidableException extends RuntimeException {
    public ApprovalNotDecidableException(String message) {
        super(message);
    }
}
