/**
 * EnhancedChatPanel — Main chat panel component with message list & input.
 *
 * Extracted from: microsoft/customer-chatbot-solution-accelerator
 *   (src/App/src/components/EnhancedChatPanel.tsx)
 * Adapted: shadcn ScrollArea/Input/Button/Skeleton → FluentUI v9 equivalents.
 *          Uses existing Coral ChatInput component for input consistency.
 *
 * Features:
 * - Message list with auto-scroll
 * - Product/order card rendering via text parsers
 * - New Chat button
 * - Loading skeleton
 * - Welcome screen when empty
 * - Typing indicator
 */
import React, { useEffect, useRef, useState, useCallback } from 'react';
import {
    Body1,
    Body1Strong,
    Button,
    Caption1,
    SkeletonItem,
    Spinner,
} from '@fluentui/react-components';
import {
    Add20Regular,
    Bot20Regular,
    Send20Regular,
} from '@fluentui/react-icons';

import ChatInput from '@/coral/modules/ChatInput';
import { EnhancedChatMessageBubble } from './EnhancedChatMessageBubble';
import type { ChatMessage, Product } from '../../lib/types';

import '../../styles/EnhancedChat.css';

interface EnhancedChatPanelProps {
    messages: ChatMessage[];
    onSendMessage: (content: string) => void;
    onNewChat: () => void;
    isTyping: boolean;
    isLoading?: boolean;
    onAddToCart?: (product: Product) => void;
    className?: string;
}

export const EnhancedChatPanel: React.FC<EnhancedChatPanelProps> = ({
    messages,
    onSendMessage,
    onNewChat,
    isTyping,
    isLoading = false,
    onAddToCart,
    className,
}) => {
    const [inputValue, setInputValue] = useState('');
    const messagesEndRef = useRef<HTMLDivElement>(null);
    const inputRef = useRef<HTMLTextAreaElement>(null);

    // ── Auto-scroll on new messages ─────────────────────────────
    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages, isTyping]);

    // ── Auto-focus input ────────────────────────────────────────
    useEffect(() => {
        if (!isTyping && !isLoading) {
            inputRef.current?.focus();
        }
    }, [isTyping, isLoading]);

    const handleSend = useCallback(() => {
        const trimmed = inputValue.trim();
        if (!trimmed || isTyping || isLoading) return;
        onSendMessage(trimmed);
        setInputValue('');
        inputRef.current?.focus();
    }, [inputValue, isTyping, isLoading, onSendMessage]);

    // ── Loading skeleton ────────────────────────────────────────
    if (isLoading && messages.length === 0) {
        return (
            <div className={`echat-panel ${className || ''}`}>
                <div className="echat-messages">
                    {[1, 2, 3].map((i) => (
                        <div key={i} style={{ padding: '16px', display: 'flex', gap: '12px' }}>
                            <SkeletonItem shape="circle" size={28} />
                            <div style={{ flex: 1 }}>
                                <SkeletonItem size={16} style={{ width: '30%', marginBottom: '8px' }} />
                                <SkeletonItem size={48} style={{ width: '80%' }} />
                            </div>
                        </div>
                    ))}
                </div>
            </div>
        );
    }

    return (
        <div className={`echat-panel ${className || ''}`}>
            {/* ── Messages area ────────────────────────────────── */}
            <div className="echat-messages">
                {messages.length === 0 && !isLoading && (
                    <div className="echat-welcome">
                        <Bot20Regular style={{ fontSize: '48px', color: 'var(--colorBrandForeground1)' }} />
                        <Body1Strong style={{ marginTop: '12px' }}>
                            How can I help you today?
                        </Body1Strong>
                        <Caption1 style={{ color: 'var(--colorNeutralForeground3)', marginTop: '4px' }}>
                            Ask a question, describe a task, or explore MCP tools.
                        </Caption1>
                    </div>
                )}

                {messages.map((msg) => (
                    <EnhancedChatMessageBubble
                        key={msg.id}
                        message={msg}
                        onAddToCart={onAddToCart}
                    />
                ))}

                {isTyping && (
                    <EnhancedChatMessageBubble
                        message={{
                            id: 'typing',
                            content: '',
                            role: 'assistant',
                            timestamp: new Date(),
                        }}
                        isTyping
                    />
                )}

                <div ref={messagesEndRef} />
            </div>

            {/* ── Input area ───────────────────────────────────── */}
            <div className="echat-input-area">
                <Caption1
                    style={{
                        textAlign: 'center',
                        color: 'var(--colorNeutralForeground3)',
                        marginBottom: '4px',
                    }}
                >
                    AI-generated content may be incorrect
                </Caption1>

                <div className="echat-input-row">
                    <Button
                        appearance="subtle"
                        icon={<Add20Regular />}
                        onClick={onNewChat}
                        title="New chat"
                        aria-label="New chat"
                        size="small"
                    />

                    <div className="echat-input-wrapper">
                        <ChatInput
                            ref={inputRef}
                            value={inputValue}
                            placeholder="Type a message…"
                            onChange={setInputValue}
                            onEnter={handleSend}
                            disabledChat={isTyping || isLoading}
                        >
                            <Button
                                appearance="subtle"
                                icon={<Send20Regular />}
                                onClick={handleSend}
                                disabled={isTyping || isLoading || !inputValue.trim()}
                                aria-label="Send"
                                size="small"
                            />
                        </ChatInput>
                    </div>
                </div>
            </div>
        </div>
    );
};

export default EnhancedChatPanel;
