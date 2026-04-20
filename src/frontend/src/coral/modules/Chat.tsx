import React, { useState, useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypePrism from "rehype-prism";
import {
  Body1,
  Button,
  Tag,
  ToolbarDivider,
} from "@fluentui/react-components";
import { Copy, Send } from "../imports/bundleicons";
import { ChatDismiss20Regular, HeartRegular } from "@fluentui/react-icons";
import ChatInput from "./ChatInput";
import WidgetFrame from "../components/WidgetFrame";
import { apiClient } from "../../api/apiClient";
import "./Chat.css";
import "./prism-material-oceanic.css";
// import { chatService } from "../services/chatService"; // TODO: Re-enable when chatService integration is complete
import HeaderTools from "../components/Header/HeaderTools";

interface Message {
  role: string;
  content: string;
  _meta?: {
    ui?: {
      resourceUri?: string;
      fallback?: 'markdown' | 'text';
    };
  };
}

// Response can be either string (legacy) or Message object with _meta
type MessageResponse = string | Message;

interface ChatProps {
  userId: string;
  children?: React.ReactNode;
  onSendMessage?: (
    input: string,
    history: Message[]
  ) => AsyncIterable<MessageResponse> | Promise<MessageResponse>;
  onSaveMessage?: (
    userId: string,
    messages: Message[]
  ) => void;
  onLoadHistory?: (
    userId: string
  ) => Promise<Message[]>;
  onClearHistory?: (userId: string) => void;
}

const Chat: React.FC<ChatProps> = ({
  userId,
  children,
  onSendMessage,
  onSaveMessage,
  onLoadHistory,
  onClearHistory,
}) => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isTyping, setIsTyping] = useState(false);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const [inputHeight, setInputHeight] = useState(0);
  const [availableWidgets, setAvailableWidgets] = useState<any[]>([]);

  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const inputContainerRef = useRef<HTMLDivElement>(null);

  // MCP Discovery: Load available widgets proactively
  useEffect(() => {
    const discoverWidgets = async () => {
      try {
        const result = await apiClient.get('/v4/mcp/discovery');
        setAvailableWidgets(result?.widgets || []);
        console.log(`✅ Discovered ${result?.widgets?.length || 0} widgets`);
      } catch (err) {
        console.warn('Widget discovery failed:', err);
      }
    };

    discoverWidgets();
  }, [userId]);

  useEffect(() => {
    const loadHistory = async () => {
      try {
        if (onLoadHistory) {
          const historyMessages = await onLoadHistory(userId);
          if (historyMessages && historyMessages.length > 0) {
            setMessages(historyMessages);
            return;
          }
        }
        // const chatMessages = await chatService.getUserHistory(userId);
        // setMessages(chatMessages);
      } catch (err) {
        console.log("Failed to load chat history.", err);
      }
    };
    loadHistory();
  }, [onLoadHistory, userId]);

  useEffect(() => {
    if (messagesContainerRef.current) {
      messagesContainerRef.current.scrollTop = messagesContainerRef.current.scrollHeight;
      setShowScrollButton(false);
    }
  }, [messages]);

  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;
    const handleScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container;
      setShowScrollButton(scrollTop + clientHeight < scrollHeight - 100);
    };
    container.addEventListener("scroll", handleScroll);
    return () => container.removeEventListener("scroll", handleScroll);
  }, []);

  useEffect(() => {
    if (inputContainerRef.current) {
      setInputHeight(inputContainerRef.current.offsetHeight);
    }
  }, [input]);

  const scrollToBottom = () => {
    messagesContainerRef.current?.scrollTo({
      top: messagesContainerRef.current.scrollHeight,
      behavior: "smooth",
    });
    setShowScrollButton(false);
  };

  const handleCopy = (text: string) => {
    navigator.clipboard.writeText(text).catch((err) => {
      console.log("Failed to copy text:", err);
    });
  };

  const isAsyncIterable = (value: any): value is AsyncIterable<any> => {
    return value !== null && typeof value === 'object' && Symbol.asyncIterator in value;
  };

  const sendMessage = async () => {
    if (!input.trim()) return;

    const updatedMessages = [...messages, { role: "user", content: input }];
    setMessages(updatedMessages);
    setInput("");
    setIsTyping(true);

    try {
      if (onSendMessage) {
        setMessages([...updatedMessages, { role: "assistant", content: "" }]);
        const response = onSendMessage(input, updatedMessages);

        if (isAsyncIterable(response)) {
          for await (const chunk of response) {
            setMessages((prev) => {
              const updated = [...prev];
              const lastMsg = updated[updated.length - 1];

              // Handle both string chunks and Message objects
              if (typeof chunk === 'string') {
                updated[updated.length - 1] = {
                  role: "assistant",
                  content: (lastMsg?.content || "") + chunk,
                };
              } else {
                // Message object with potential _meta
                updated[updated.length - 1] = {
                  role: "assistant",
                  content: (lastMsg?.content || "") + chunk.content,
                  _meta: chunk._meta || lastMsg._meta,
                };
              }
              return updated;
            });
          }
          // Get final message from state for saving
          setMessages((prev) => {
            const finalMsg = prev[prev.length - 1];
            onSaveMessage?.(userId, [...updatedMessages, finalMsg]);
            return prev;
          });
        } else {
          const assistantResponse = await response;

          // Handle both string response and Message object
          const assistantMessage: Message = typeof assistantResponse === 'string'
            ? { role: "assistant", content: assistantResponse }
            : { role: "assistant", content: assistantResponse.content, _meta: assistantResponse._meta };

          const newHistory = [...updatedMessages, assistantMessage];
          setMessages(newHistory);
          onSaveMessage?.(userId, newHistory);
        }
      } else {
        // TODO: Implement chatService integration when not using onSendMessage
        // const response = await chatService.sendMessage(userId, input, currentConversationId);
        // setCurrentConversationId(response.conversation_id);
        // const assistantMessage = { role: "assistant", content: response.assistant_response };
        // setMessages([...updatedMessages, assistantMessage]);
        console.warn("No onSendMessage handler provided, message not sent");
      }
    } catch (err) {
      console.log("Send Message Error:", err);
      setMessages([
        ...updatedMessages,
        { role: "assistant", content: "Oops! Something went wrong sending your message." },
      ]);
    } finally {
      setIsTyping(false);
    }
  };

  const clearChat = async () => {
    try {
      if (onClearHistory) {
        onClearHistory(userId);
      } else {
        // await chatService.clearChatHistory(userId);
      }
      setMessages([]);
    } catch (err) {
      console.log("Failed to clear chat history:", err);
    }
  };

  return (
    <div className="chat-container">
      <div className="messages" ref={messagesContainerRef}>
        <div className="message-wrapper">
        {/* Proactive Discovery: Quick Widgets panel */}
        {messages.length === 0 && availableWidgets.length > 0 && (
          <div style={{
            padding: '16px',
            marginBottom: '16px',
            borderRadius: '12px',
            background: 'linear-gradient(135deg, #f0f4ff 0%, #e8f0fe 100%)',
            border: '1px solid #d0daf0',
          }}>
            <div style={{ fontSize: '14px', fontWeight: 600, color: '#1a1a1a', marginBottom: '12px' }}>
              🧩 Available Widgets
            </div>
            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
              {availableWidgets
                .filter((w: any) => w.proactive !== false)
                .map((w: any, i: number) => (
                  <button
                    key={i}
                    onClick={() => {
                      // For proactive widgets, render directly via WidgetFrame
                      const widgetMsg: Message = {
                        role: 'assistant',
                        content: w.description || w.title,
                        _meta: { ui: { resourceUri: w.resource_uri, fallback: 'markdown' } },
                      };
                      setMessages((prev) => [...prev, widgetMsg]);
                    }}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '6px',
                      padding: '8px 14px',
                      borderRadius: '8px',
                      border: '1px solid #c7d2e0',
                      background: '#ffffff',
                      cursor: 'pointer',
                      fontSize: '13px',
                      color: '#374151',
                      transition: 'box-shadow 0.15s ease',
                      boxShadow: '0 1px 2px rgba(0,0,0,0.06)',
                    }}
                    onMouseEnter={(e) => (e.currentTarget.style.boxShadow = '0 2px 8px rgba(0,0,0,0.12)')}
                    onMouseLeave={(e) => (e.currentTarget.style.boxShadow = '0 1px 2px rgba(0,0,0,0.06)')}
                  >
                    <span>{w.icon || '🔧'}</span>
                    <span>{w.title}</span>
                  </button>
                ))}
            </div>
            {availableWidgets.some((w: any) => w.proactive === false) && (
              <div style={{ marginTop: '8px', fontSize: '12px', color: '#6b7280' }}>
                💡 More widgets available via AI — try asking about products!
              </div>
            )}
          </div>
        )}
        {messages.map((msg, index) => (
          <div key={index} className={`message ${msg.role}`}>
            <Body1>
              <div style={{ display: "flex", flexDirection: "column", whiteSpace: "pre-wrap", width: "100%" }}>
                {/* MCP Protocol 2025-11-25: Render widget if _meta.ui.resourceUri present */}
                {msg._meta?.ui?.resourceUri ? (
                  <WidgetFrame
                    resourceUri={msg._meta.ui.resourceUri}
                    fallbackContent={msg.content}
                    fallbackFormat={msg._meta.ui.fallback || 'markdown'}
                  />
                ) : (
                  <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypePrism]}>
                    {msg.content}
                  </ReactMarkdown>
                )}
                {msg.role === "assistant" && (
                  <div className="assistant-footer">
                    <div className="assistant-actions">
                      <Button
                        onClick={() => handleCopy(msg.content)}
                        title="Copy Response"
                        appearance="subtle"
                        style={{ height: 28, width: 28 }}
                        icon={<Copy />}
                      />
                      <Button
                        onClick={() => console.log("Heart clicked for response:", msg.content)}
                        title="Like"
                        appearance="subtle"
                        style={{ height: 28, width: 28 }}
                        icon={<HeartRegular />}
                      />
                    </div>
                  </div>
                )}
              </div>
            </Body1>
          </div>
        ))}</div>


        {isTyping && (
          <div className="typing-indicator">
            <span>Thinking...</span>
          </div>
        )}
      </div>

      {showScrollButton && (
        <Tag
          onClick={scrollToBottom}
          className="scroll-to-bottom"
          shape="circular"
          style={{
            bottom: inputHeight,
            backgroundColor: "transparent",
            border: '1px solid var(--colorNeutralStroke3)',
            backdropFilter: "saturate(180%) blur(16px)",
          }}
        >
          Back to bottom
        </Tag>
      )}

      <div ref={inputContainerRef} style={{ display: 'flex', width: '100%', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ display: 'flex', width: '100%', maxWidth: '768px', margin: '0px 16px' }}>
          <ChatInput
            value={input}
            onChange={setInput}
            onEnter={sendMessage}

          >
            <Button
              appearance="transparent"
              onClick={sendMessage}
              icon={<Send />}
              aria-label="Send message"
              disabled={isTyping || !input.trim()}
            />

            {messages.length > 0 && (
              <HeaderTools>
                <ToolbarDivider />
                <Button
                  onClick={clearChat}
                  appearance="transparent"
                  icon={<ChatDismiss20Regular />}
                  aria-label="Clear chat"
                  disabled={isTyping || messages.length === 0} />

              </HeaderTools>

            )}

          </ChatInput>
        </div>

      </div>


    </div>
  );
};

export default Chat;
