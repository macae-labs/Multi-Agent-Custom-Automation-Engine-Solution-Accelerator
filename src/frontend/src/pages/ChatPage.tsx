/**
 * ChatPage — Full page for conversational mode (P0)
 *
 * Layout: PanelLeft (sidebar with recent chats) + Content (EnhancedChatPanel)
 * Accessible via route "/chat/:sessionId"
 *
 * Uses SSE streaming: POST /v4/chat/message/stream returns token-by-token
 * events so the assistant response renders incrementally.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';

import '../styles/PlanPage.css';

import CoralShellColumn from '../coral/components/Layout/CoralShellColumn';
import CoralShellRow from '../coral/components/Layout/CoralShellRow';
import Content from '../coral/components/Content/Content';
import ContentToolbar from '@/coral/components/Content/ContentToolbar';
import PlanPanelLeft from '@/components/content/PlanPanelLeft';
import InspectorLink from '@/components/inspector/InspectorLink';
import InlineToaster from '../components/toast/InlineToaster';

import { EnhancedChatPanel } from '@/components/chat/EnhancedChatPanel';
import { ChatService } from '../services/ChatService';
import { NewTaskService } from '../services/NewTaskService';
import type { ChatMessage } from '../lib/types';

const ChatPage: React.FC = () => {
    const { sessionId } = useParams<{ sessionId: string }>();
    const navigate = useNavigate();
    const location = useLocation();

    // Messages state
    const [messages, setMessages] = useState<ChatMessage[]>([]);
    const [isTyping, setIsTyping] = useState(false);
    const [isLoading, setIsLoading] = useState(false);

    // Load initial messages from navigation state or Cosmos
    useEffect(() => {
        const stateMessages = (location.state as any)?.initialMessages;
        if (stateMessages && stateMessages.length > 0) {
            const converted = stateMessages.map((m: any) => ({
                ...m,
                role: m.role || m.sender || 'user',
                timestamp: m.timestamp instanceof Date ? m.timestamp : new Date(m.timestamp),
            }));
            setMessages(converted);
        } else if (sessionId) {
            const cached = ChatService.getMessages(sessionId);
            if (cached.length > 0) {
                setMessages(cached);
            } else {
                setIsLoading(true);
                ChatService.loadSession(sessionId)
                    .then((msgs) => setMessages(msgs))
                    .finally(() => setIsLoading(false));
            }
        }
    }, [sessionId, location.state]);

    // ── Send message handler (SSE streaming) ────────────────────
    const handleSendMessage = useCallback(async (content: string) => {
        if (!content.trim() || isTyping) return;

        // Optimistically add user message
        const userMsg: ChatMessage = {
            id: `msg_${Date.now()}_user`,
            content,
            role: 'user',
            timestamp: new Date(),
        };
        setMessages((prev) => [...prev, userMsg]);

        // Create placeholder assistant message — filled token by token
        const assistantMsgId = `msg_${Date.now()}_assistant`;
        setMessages((prev) => [
            ...prev,
            {
                id: assistantMsgId,
                content: '',
                role: 'assistant',
                timestamp: new Date(),
            },
        ]);
        setIsTyping(true);

        try {
            await ChatService.sendMessageStream(content, sessionId, {
                onToken: (token) => {
                    setMessages((prev) =>
                        prev.map((m) =>
                            m.id === assistantMsgId
                                ? { ...m, content: m.content + token }
                                : m,
                        ),
                    );
                },
                onToolActivity: (data) => {
                    setMessages((prev) =>
                        prev.map((m) =>
                            m.id === assistantMsgId
                                ? {
                                    ...m,
                                    toolActivity: [
                                        ...(m.toolActivity || []),
                                        {
                                            activity: data.activity as 'calling' | 'result' | 'thinking',
                                            tool: data.tool,
                                            server: data.server,
                                            success: data.success,
                                        },
                                    ],
                                }
                                : m,
                        ),
                    );
                },
                onIntent: (data) => {
                    if (data.session_id && data.session_id !== sessionId) {
                        navigate(`/chat/${data.session_id}`, { replace: true });
                    }
                },
                onDone: (data) => {
                    setMessages((prev) =>
                        prev.map((m) =>
                            m.id === assistantMsgId
                                ? { ...m, intent: data.intent, agent: data.agent, confidence: data.confidence }
                                : m,
                        ),
                    );
                },
                onPlanCreated: (planId) => {
                    navigate(`/plan/${planId}`);
                },
                onError: (errorMsg) => {
                    setMessages((prev) =>
                        prev.map((m) =>
                            m.id === assistantMsgId
                                ? { ...m, content: `Error: ${errorMsg}` }
                                : m,
                        ),
                    );
                },
            });
        } catch (error: any) {
            console.error('Chat stream error:', error);
            setMessages((prev) =>
                prev.map((m) =>
                    m.id === assistantMsgId
                        ? { ...m, content: `Error: ${error?.message || 'Failed to send message'}` }
                        : m,
                ),
            );
        } finally {
            setIsTyping(false);
        }
    }, [sessionId, isTyping, navigate]);

    // ── New chat handler ────────────────────────────────────────
    const handleNewChat = useCallback(async () => {
        const newId = await ChatService.createNewSession();
        setMessages([]);
        navigate(`/chat/${newId}`);
    }, [navigate]);

    const handleNewTaskButton = useCallback(() => {
        navigate('/', { state: { focusInput: true } });
    }, [navigate]);

    return (
        <>
            <InlineToaster />
            <CoralShellColumn>
                <CoralShellRow>
                    <PlanPanelLeft
                        reloadTasks={true}
                        onNewTaskButton={handleNewTaskButton}
                        isHomePage={false}
                    />
                    <Content>
                        <ContentToolbar panelTitle="Chat">
                            <InspectorLink />
                        </ContentToolbar>
                        <EnhancedChatPanel
                            messages={messages}
                            onSendMessage={handleSendMessage}
                            onNewChat={handleNewChat}
                            isTyping={isTyping}
                            isLoading={isLoading}
                        />
                    </Content>
                </CoralShellRow>
            </CoralShellColumn>
        </>
    );
};

export default ChatPage;
