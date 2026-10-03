import React from 'react';

import { renderMarkdown } from '@grafana/data';
import { config } from '@grafana/runtime';

import type { ChatMessage } from './api';

export function ChatSurfaceStyles() {
  return <style>{`
    .mkhco-chat-content { font-size: 14px; line-height: 1.75; overflow-wrap: anywhere; text-align: start; unicode-bidi: plaintext; }
    .mkhco-chat-content > :first-child { margin-top: 0; }
    .mkhco-chat-content > :last-child { margin-bottom: 0; }
    .mkhco-chat-content p { margin: 0 0 10px; }
    .mkhco-chat-content h1, .mkhco-chat-content h2, .mkhco-chat-content h3,
    .mkhco-chat-content h4 { margin: 14px 0 8px; line-height: 1.45; }
    .mkhco-chat-content ul, .mkhco-chat-content ol { margin: 6px 0 12px; padding-inline-start: 24px; }
    .mkhco-chat-content li { margin: 4px 0; }
    .mkhco-chat-content code { direction: ltr; unicode-bidi: isolate; display: inline-block; max-width: 100%;
      padding: 1px 5px; border-radius: 4px; background: rgba(128,128,128,.16); font-size: .9em; }
    .mkhco-chat-content pre { direction: ltr; text-align: left; overflow-x: auto; margin: 10px 0; padding: 12px;
      border: 1px solid rgba(128,128,128,.25); border-radius: 7px; background: rgba(0,0,0,.18); }
    .mkhco-chat-content pre code { display: inline; padding: 0; background: transparent; }
    .mkhco-chat-content table { display: block; max-width: 100%; overflow-x: auto; direction: rtl;
      border-collapse: collapse; margin: 10px 0; }
    .mkhco-chat-content th, .mkhco-chat-content td { min-width: 110px; padding: 7px 9px; text-align: start;
      vertical-align: top; border: 1px solid rgba(128,128,128,.28); }
    .mkhco-chat-content blockquote { margin: 10px 0; padding-inline-start: 12px;
      border-inline-start: 3px solid rgba(87,148,242,.65); opacity: .9; }
    .mkhco-chat-content a { color: #6aa9ff; }
  `}</style>;
}

export function ChatMessageBubble({ message, compact = false }: { message: ChatMessage; compact?: boolean }) {
  const colors = config.theme2.colors;
  const isUser = message.role === 'user';
  return (
    <div
      data-testid={`chat-message-${message.role}`}
      style={{
        alignSelf: isUser ? 'flex-end' : 'flex-start',
        width: isUser ? 'auto' : '94%',
        maxWidth: isUser ? (compact ? '88%' : '86%') : '94%',
        display: 'flex',
        flexDirection: 'column',
        gap: 5,
      }}
    >
      <span style={{ paddingInline: 5, fontSize: 11, opacity: 0.62 }}>
        {isUser ? 'شما' : 'دستیار Grafana'}
      </span>
      <div
        dir="auto"
        className="mkhco-chat-content"
        style={{
          padding: compact ? '10px 12px' : '12px 14px',
          borderRadius: isUser ? '14px 14px 4px 14px' : '14px 14px 14px 4px',
          border: `1px solid ${isUser ? colors.primary.border : colors.border.weak}`,
          background: isUser ? colors.primary.transparent : colors.background.secondary,
          boxShadow: isUser ? 'none' : '0 2px 10px rgba(0,0,0,.08)',
        }}
        dangerouslySetInnerHTML={{ __html: renderMarkdown(message.content, { breaks: true }) }}
      />
    </div>
  );
}
