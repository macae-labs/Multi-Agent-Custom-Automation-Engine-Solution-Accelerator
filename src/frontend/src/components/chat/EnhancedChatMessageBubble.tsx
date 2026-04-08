/**
 * EnhancedChatMessageBubble — Renders a single chat message with rich content.
 *
 * Extracted from: microsoft/customer-chatbot-solution-accelerator
 *   (src/App/src/components/EnhancedChatMessageBubble.tsx)
 * Adapted: shadcn Avatar → FluentUI Avatar, Tailwind → CSS variables.
 *
 * Key feature: detects content type (orders/products/text) from AI markdown
 * responses and renders structured cards inline.
 */
import React, { memo } from 'react';
import {
    Avatar,
    Body1,
    Caption1,
    Badge,
} from '@fluentui/react-components';
import {
    Bot20Regular,
    Person20Regular,
} from '@fluentui/react-icons';

import { detectContentType, parseOrdersFromText, parseProductsFromText, formatTimestamp } from '../../lib/textParsers';
import type { ChatMessage, Product } from '../../lib/types';
import { ChatProductCard } from './ChatProductCard';
import { ChatOrderCard } from './ChatOrderCard';

import '../../styles/EnhancedChat.css';

interface EnhancedChatMessageBubbleProps {
    message: ChatMessage;
    isTyping?: boolean;
    onAddToCart?: (product: Product) => void;
}

const EnhancedChatMessageBubbleInner: React.FC<EnhancedChatMessageBubbleProps> = ({
    message,
    isTyping,
    onAddToCart,
}) => {
    const isUser = message.role === 'user';
    const isAssistant = message.role === 'assistant';

    // ── Typing indicator ────────────────────────────────────────
    if (isTyping) {
        return (
            <div className="echat-bubble echat-bubble--assistant">
                <Avatar
                    icon={<Bot20Regular />}
                    color="brand"
                    size={28}
                    className="echat-avatar"
                />
                <div className="echat-bubble-body">
                    <div className="echat-typing-dots">
                        <span /><span /><span />
                    </div>
                </div>
            </div>
        );
    }

    // ── Content rendering ───────────────────────────────────────
    const contentType = isAssistant ? detectContentType(message.content) : 'text';

    const renderContent = () => {
        if (contentType === 'orders') {
            const { orders, introText } = parseOrdersFromText(message.content);
            return (
                <>
                    {introText && (
                        <Body1 style={{ whiteSpace: 'pre-wrap', marginBottom: '8px' }}>{introText}</Body1>
                    )}
                    {orders.map((order, idx) => (
                        <ChatOrderCard key={idx} order={order} />
                    ))}
                </>
            );
        }

        if (contentType === 'products') {
            const { products, introText, outroText } = parseProductsFromText(message.content);
            return (
                <>
                    {introText && (
                        <Body1 style={{ whiteSpace: 'pre-wrap', marginBottom: '8px' }}>{introText}</Body1>
                    )}
                    {products.map((product, idx) => (
                        <ChatProductCard
                            key={idx}
                            product={product}
                            onAddToCart={onAddToCart}
                        />
                    ))}
                    {outroText && (
                        <Body1 style={{ whiteSpace: 'pre-wrap', marginTop: '8px' }}>{outroText}</Body1>
                    )}
                </>
            );
        }

        // Legacy: check for pre-parsed data on message object
        if (message.recommendedProducts && message.recommendedProducts.length > 0) {
            return (
                <>
                    <Body1 style={{ whiteSpace: 'pre-wrap' }}>{message.content}</Body1>
                    {message.recommendedProducts.map((product, idx) => (
                        <ChatProductCard
                            key={idx}
                            product={product}
                            onAddToCart={onAddToCart}
                        />
                    ))}
                </>
            );
        }

        // Default: plain text
        return <Body1 style={{ whiteSpace: 'pre-wrap' }}>{message.content}</Body1>;
    };

    // ── Intent badge (MACAE-specific) ───────────────────────────
    const intentBadge = isAssistant && message.intent ? (
        <Badge
            appearance="outline"
            color={
                message.intent === 'mcp_query' ? 'success' :
                message.intent === 'task' ? 'warning' : 'informative'
            }
            size="small"
        >
            {message.intent === 'mcp_query' ? 'MCP' : message.intent}
        </Badge>
    ) : null;

    return (
        <div className={`echat-bubble echat-bubble--${isUser ? 'user' : 'assistant'}`}>
            <Avatar
                icon={isUser ? <Person20Regular /> : <Bot20Regular />}
                color={isUser ? 'neutral' : 'brand'}
                size={28}
                className="echat-avatar"
            />
            <div className="echat-bubble-body">
                <div className="echat-bubble-header">
                    <Caption1 style={{ fontWeight: 600 }}>
                        {isUser ? 'You' : (message.agent || 'Assistant')}
                    </Caption1>
                    <div className="echat-bubble-meta">
                        {intentBadge}
                        <Caption1 style={{ color: 'var(--colorNeutralForeground3)' }}>
                            {formatTimestamp(message.timestamp)}
                        </Caption1>
                    </div>
                </div>
                <div className="echat-bubble-content">
                    {renderContent()}
                </div>
            </div>
        </div>
    );
};

export const EnhancedChatMessageBubble = memo(EnhancedChatMessageBubbleInner);
export default EnhancedChatMessageBubble;
