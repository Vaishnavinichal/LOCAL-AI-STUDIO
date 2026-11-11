import { useEffect, useRef, useState } from "react";
import {
  Sparkles, Paperclip, ArrowUp, ChevronDown, User, Bot, LoaderCircle,
  Copy, RefreshCw, Share2, FolderPlus, ThumbsUp, ThumbsDown, Check
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import type { ChatMessage } from "../../types/chat";

interface ChatAreaProps {
  title: string;
  messages: ChatMessage[];
  onSend: (content: string) => Promise<void>;
  onFeedback: (messageId: string, rating: "good" | "bad" | null) => Promise<void>;
  onRegenerate: (messageId: string) => Promise<void>;
  onAddToProject: (messageId: string) => Promise<void>;
  modelName: string;
  loading: boolean;
  generating: boolean;
  error: string;
}
function formatTimestamp(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export default function ChatArea({
  title, messages, onSend, onFeedback, onRegenerate, onAddToProject,
  modelName, loading, generating, error
}: ChatAreaProps) {
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const pane = scrollRef.current;
    if (pane) pane.scrollTo({ top: pane.scrollHeight, behavior: "smooth" });
  }, [messages]);

  async function handleSend() {
    const content = message.trim();
    if (!content || sending || generating || loading) return;
    setMessage(""); setSending(true);
    try { await onSend(content); }
    catch { setMessage(content); }
    finally { setSending(false); }
  }
  async function copyText(item: ChatMessage) {
    try {
      await navigator.clipboard.writeText(item.content);
      setCopiedId(item.id);
      window.setTimeout(() => setCopiedId(current => current === item.id ? null : current), 1600);
    } catch {}
  }
  async function shareText(item: ChatMessage) {
    try {
      if (navigator.share) await navigator.share({ title: "Local AI Studio response", text: item.content });
      else await copyText(item);
    } catch {}
  }

  const markdownComponents: Components = {
    code({ className, children, ...props }) {
      const match = /language-([\w-]+)/.exec(className || "");
      const code = String(children).replace(/\n$/, "");
      if (match) return (
        <SyntaxHighlighter language={match[1]} style={oneDark} showLineNumbers wrapLongLines={false}
          customStyle={{ margin: "12px 0", borderRadius: 10, fontSize: 13, lineHeight: 1.65, background: "#15171b", border: "1px solid #34363d" }}
          codeTagProps={{ style: { fontFamily: '"Cascadia Code", Consolas, monospace' } }}>
          {code}
        </SyntaxHighlighter>
      );
      return <code className={`inline-code ${className || ""}`} {...props}>{children}</code>;
    },
    pre({ children }) { return <>{children}</>; }
  };
  const busy = sending || generating || loading;

  return (
    <main className="chat-area">
      <header className="topbar">
        <span className="topbar-title">{title}</span>
        <button className="model-selector" type="button" disabled title="Currently loaded local model">
          <Sparkles size={16} /> {modelName || "Local model"} <ChevronDown size={15} />
        </button>
      </header>
      <section className="chat-content">
        {messages.length === 0 && !loading ? (
          <div className="welcome">
            <div className="welcome-icon"><Sparkles size={28} /></div>
            <h1>What can I help with?</h1>
            <p>Your private AI workspace. Everything runs locally.</p>
          </div>
        ) : (
          <div className="messages-container" ref={scrollRef}>
            {messages.map(item => {
              const isUser = item.role === "user";
              const pending = generating && item.role === "assistant" && item.id.startsWith("local-assistant-") && !item.content;
              return (
                <article className={`message-row ${item.role}`} key={item.id}>
                  <div className="message-avatar">{isUser ? <User size={17} /> : <Bot size={17} />}</div>
                  <div className="message-body">
                    <div className="message-heading">
                      <strong>{isUser ? "You" : "Assistant"}</strong>
                      {!isUser && <span className="message-model"><Sparkles size={12} /> {item.model_name || modelName || "Local model"}</span>}
                    </div>
                    {pending ? <span className="generating-indicator"><LoaderCircle size={15} className="spinner" /> Thinking...</span> :
                      <div className="message-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{item.content}</ReactMarkdown></div>}
                    {!isUser && item.content && !item.id.startsWith("local-assistant-") && (
                      <div className="assistant-message-footer">
                        <div className="message-toolbar">
                          <button type="button" onClick={() => copyText(item)} title="Copy response">
                            {copiedId === item.id ? <Check size={15}/> : <Copy size={15}/>}<span>{copiedId === item.id ? "Copied" : "Copy"}</span>
                          </button>
                          <button type="button" disabled={busy} onClick={() => onRegenerate(item.id)} title="Regenerate response">
                            <RefreshCw size={15}/><span>Regenerate</span>
                          </button>
                          <button type="button" onClick={() => shareText(item)} title="Share response"><Share2 size={15}/><span>Share</span></button>
                          <button type="button" disabled={busy} onClick={() => onAddToProject(item.id)} title="Add response to project">
                            <FolderPlus size={15}/><span>Add to project</span>
                          </button>
                          <button type="button" className={item.feedback === "good" ? "feedback-selected" : ""}
                            onClick={() => onFeedback(item.id, item.feedback === "good" ? null : "good")} aria-pressed={item.feedback === "good"}>
                            <ThumbsUp size={15}/><span>Good</span>
                          </button>
                          <button type="button" className={item.feedback === "bad" ? "feedback-selected" : ""}
                            onClick={() => onFeedback(item.id, item.feedback === "bad" ? null : "bad")} aria-pressed={item.feedback === "bad"}>
                            <ThumbsDown size={15}/><span>Bad</span>
                          </button>
                        </div>
                        <div className="message-timestamp">Generated {formatTimestamp(item.created_at)}</div>
                      </div>
                    )}
                  </div>
                </article>
              );
            })}
            {loading && <div className="loading-message"><LoaderCircle size={17} className="spinner"/> Loading conversation...</div>}
          </div>
        )}
        {error && <div className="error-message">{error}</div>}
        <div className="composer-container">
          <div className="composer">
            <textarea value={message} onChange={event => setMessage(event.target.value)}
              onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); handleSend(); } }}
              placeholder="Message Local AI Studio..." rows={2} disabled={busy}/>
            <div className="composer-actions">
              <button className="attachment-button" type="button" title="Attachments (coming soon)" disabled><Paperclip size={19}/></button>
              <button className="send-button" type="button" disabled={!message.trim() || busy} onClick={handleSend}>
                {busy ? <LoaderCircle size={18} className="spinner"/> : <ArrowUp size={19}/>}
              </button>
            </div>
          </div>
          <p className="composer-note">{generating ? `${modelName || "Local model"} is generating...` : "Your messages and AI responses are stored locally."}</p>
        </div>
      </section>
    </main>
  );
}
