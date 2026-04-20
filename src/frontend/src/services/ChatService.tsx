/**
 * ChatService — manages conversational chat sessions (P0)
 *
 * Handles:
 * - Sending messages through IntentRouter
 * - Managing local chat session state
 * - Persisting/loading sessions via Cosmos DB endpoints
 *
 * Extracted patterns from: microsoft/customer-chatbot-solution-accelerator
 *   (src/App/src/lib/api.ts — session management)
 */
import { apiService } from '../api/apiService';
import {
    ChatMessageRequest,
    ChatMessageResponse,
} from '../models/chatMessage';
import type {
    ChatMessage,
    ChatSessionSummary,
    ChatSessionFull,
} from '../lib/types';

/** Callbacks for streaming chat responses. */
export interface StreamCallbacks {
    onToken: (token: string) => void;
    onIntent: (data: { intent: string; confidence: number; session_id: string }) => void;
    onDone: (data: { intent: string; agent: string; confidence: number; session_id: string }) => void;
    /** Called when intent router detects a task and creates a plan inline. */
    onPlanCreated?: (planId: string) => void;
    /** Legacy redirect — kept for backward compat. */
    onRedirect?: (planId: string) => void;
    onError: (error: string) => void;
    onToolActivity?: (data: { activity: string; tool: string; server?: string; success?: boolean }) => void;
}

// In-memory cache of chat sessions (persists during page lifetime)
const chatSessions = new Map<string, ChatMessage[]>();

export class ChatService {
    /**
     * Send a message and stream the response via SSE.
     * The LLM tokens arrive one-by-one via callbacks.
     */
    static async sendMessageStream(
        message: string,
        sessionId: string | undefined,
        callbacks: StreamCallbacks,
    ): Promise<void> {
        const request: ChatMessageRequest = {
            session_id: sessionId || '',
            message,
        };
        await apiService.sendChatMessageStream(request, callbacks);
    }

    /**
     * Send a message to the IntentRouter endpoint (non-streaming fallback).
     * Returns the full response including intent classification.
     */
    static async sendMessage(
        message: string,
        sessionId?: string,
    ): Promise<ChatMessageResponse> {
        const request: ChatMessageRequest = {
            session_id: sessionId || '',
            message,
        };

        const response = await apiService.sendChatMessage(request);

        // Store messages in local cache
        const sid = response.session_id;
        const msgs = ChatService.getMessages(sid);

        // Add user message
        msgs.push({
            id: `msg_${Date.now()}_user`,
            content: message,
            role: 'user',
            timestamp: new Date(),
        });

        // Add assistant response (only for non-task intents)
        if (response.intent !== 'task') {
            msgs.push({
                id: `msg_${Date.now()}_assistant`,
                content: response.response,
                role: 'assistant',
                timestamp: new Date(),
                intent: response.intent,
                agent: response.agent,
                confidence: response.confidence,
            });
        }

        chatSessions.set(sid, msgs);
        return response;
    }

    /**
     * Get all messages for a session (local cache).
     */
    static getMessages(sessionId: string): ChatMessage[] {
        if (!chatSessions.has(sessionId)) {
            chatSessions.set(sessionId, []);
        }
        return chatSessions.get(sessionId)!;
    }

    /**
     * Load session from Cosmos DB into local cache.
     */
    static async loadSession(sessionId: string): Promise<ChatMessage[]> {
        try {
            const session = await apiService.getChatSession(sessionId);
            if (!session || !session.messages) return [];

            const messages: ChatMessage[] = session.messages.map((m) => ({
                id: m.id,
                content: m.content,
                role: (m as any).role || (m as any).sender as 'user' | 'assistant',
                timestamp: new Date(m.timestamp),
                intent: (m.metadata as any)?.intent,
                agent: (m.metadata as any)?.agent,
            }));

            chatSessions.set(sessionId, messages);
            return messages;
        } catch {
            return ChatService.getMessages(sessionId);
        }
    }

    /**
     * Get all recent chat sessions from Cosmos DB.
     */
    static async getRecentSessions(): Promise<ChatSessionSummary[]> {
        try {
            const result = await apiService.getChatSessions();
            return result.sessions || [];
        } catch {
            return [];
        }
    }

    /**
     * Create a new chat session via backend.
     */
    static async createNewSession(): Promise<string> {
        try {
            const result = await apiService.createChatSession();
            return result.data.session_id;
        } catch {
            return ChatService.generateSessionId();
        }
    }

    /**
     * Delete a chat session.
     */
    static async deleteSession(sessionId: string): Promise<boolean> {
        chatSessions.delete(sessionId);
        try {
            await apiService.deleteChatSession(sessionId);
            return true;
        } catch {
            return false;
        }
    }

    /**
     * Check if a session exists in local cache.
     */
    static hasSession(sessionId: string): boolean {
        return chatSessions.has(sessionId) && chatSessions.get(sessionId)!.length > 0;
    }

    /**
     * Generate a unique session ID for chat (fallback).
     */
    static generateSessionId(): string {
        const timestamp = Date.now();
        const random = Math.floor(Math.random() * 10000);
        return `chat_${timestamp}_${random}`;
    }
}

export default ChatService;
