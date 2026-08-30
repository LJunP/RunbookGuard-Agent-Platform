package io.runbookguard.controlplane.service;

public class TerminalStateAlreadySetException extends RuntimeException {
    public TerminalStateAlreadySetException(String message) {
        super(message);
    }
}
