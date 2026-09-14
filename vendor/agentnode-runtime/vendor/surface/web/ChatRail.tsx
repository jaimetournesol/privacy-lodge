import { memo, useEffect, useRef, useState } from 'react';
import type { ChatAnnotation, ChatMessage } from '../shared/protocol';
import { Md } from './registry/Markdown';
import type { Surface } from './useSurface';
import { prettyTool } from './App';

export function ChatRail({
  surface,
  annotations,
  onRemoveAnnotation,
  onClearAnnotations,
}: {
  surface: Surface;
  annotations: ChatAnnotation[];
  onRemoveAnnotation: (index: number) => void;
  onClearAnnotations: () => void;
}) {
  const { state, send, uploadFiles } = surface;
  const [draft, setDraft] = useState('');
  const [micOn, setMicOn] = useState(false);
  const [voiceOn, setVoiceOn] = useState(() => {
    try {
      return localStorage.getItem('surface-voice') === '1';
    } catch {
      return false;
    }
  });
  const scrollRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const recRef = useRef<any>(null);
  const spoken = useRef<Set<string> | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const chat = state?.chat ?? [];
  const agent = state?.agent;

  // Speak newly finalized assistant replies when voice-out is on. History that
  // was already there on mount is seeded as "spoken" so we never read backlog.
  useEffect(() => {
    if (spoken.current === null && chat.length > 0) {
      spoken.current = new Set(chat.map((m) => m.id));
      return;
    }
    if (!spoken.current) spoken.current = new Set();
    const fresh = chat.filter((m) => m.role === 'assistant' && !m.streaming && m.text.trim() && !spoken.current!.has(m.id));
    for (const m of fresh) spoken.current.add(m.id);
    const last = fresh.at(-1);
    if (!voiceOn || !last) return;
    void (async () => {
      try {
        const res = await fetch('/api/tts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: last.text }),
        });
        if (!res.ok) return;
        const blob = await res.blob();
        audioRef.current?.pause();
        const audio = new Audio(URL.createObjectURL(blob));
        audioRef.current = audio;
        void audio.play().catch(() => {});
      } catch {
        /* tts unavailable */
      }
    })();
  }, [chat.length, chat.at(-1)?.streaming, voiceOn]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggleMic = () => {
    if (micOn) {
      recRef.current?.stop();
      return;
    }
    const SR = (window as any).SpeechRecognition ?? (window as any).webkitSpeechRecognition;
    if (!SR) {
      alert('Speech recognition is not available in this browser.');
      return;
    }
    const rec = new SR();
    rec.lang = navigator.language || 'en-US';
    rec.continuous = true;
    rec.interimResults = true;
    rec.onresult = (e: any) => {
      let finalText = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) finalText += e.results[i][0].transcript;
      }
      if (finalText) setDraft((d) => (d ? d + ' ' : '') + finalText.trim());
    };
    rec.onend = () => setMicOn(false);
    rec.onerror = () => setMicOn(false);
    recRef.current = rec;
    rec.start();
    setMicOn(true);
  };

  const toggleVoice = () => {
    setVoiceOn((v) => {
      const next = !v;
      try {
        localStorage.setItem('surface-voice', next ? '1' : '0');
      } catch {
        /* fine */
      }
      if (!next) audioRef.current?.pause();
      return next;
    });
  };

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chat.length, chat[chat.length - 1]?.text]);

  const submit = () => {
    const text = draft.trim();
    if (!text) return;
    send({ type: 'chat', text, annotations: annotations.length ? annotations : undefined });
    onClearAnnotations();
    setDraft('');
  };

  const busy = agent && ['thinking', 'tool'].includes(agent.phase);

  return (
    <aside className="chat">
      <div className="chat-scroll" ref={scrollRef}>
        {chat.length === 0 && (
          <div className="chat-empty">
            <div className="chat-empty-mark">◈</div>
            <p>This surface is shared between you and the agent.</p>
            <p className="muted">Ask for anything — an app, an analysis, a design, a file. Drop files anywhere.</p>
          </div>
        )}
        {chat.map((m) => (
          <Bubble key={m.id} role={m.role} text={m.text} streaming={m.streaming ?? false} />
        ))}
        {busy && (
          <div className="chat-status">
            <span className="spinner" />
            {agent.phase === 'tool' && agent.activeTool ? prettyTool(agent.activeTool) : 'thinking…'}
          </div>
        )}
      </div>
      {annotations.length > 0 && (
        <div className="ann-chips">
          {annotations.map((a, i) => (
            <span key={i} className="ann-chip" title={`${a.marks.length} mark(s) on ${a.componentId}`}>
              ✎ {a.title ?? a.componentType}
              <button className="ann-chip-x" onClick={() => onRemoveAnnotation(i)}>
                ✕
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="composer">
        <textarea
          value={draft}
          placeholder="Talk to the agent…"
          rows={Math.min(14, Math.max(6, draft.split('\n').length))}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="composer-tools">
          <button className="icon-btn" title="Attach files" onClick={() => fileRef.current?.click()}>
            ⏚
          </button>
          <button className={`icon-btn mic-btn ${micOn ? 'live' : ''}`} title={micOn ? 'Stop dictating' : 'Dictate'} onClick={toggleMic}>
            {micOn ? '◉' : '○'}
          </button>
          <button className={`icon-btn ${voiceOn ? 'voice-on' : ''}`} title={voiceOn ? 'Spoken replies: on' : 'Spoken replies: off'} onClick={toggleVoice}>
            {voiceOn ? '🔊' : '🔇'}
          </button>
          <input
            ref={fileRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files?.length) void uploadFiles(e.target.files);
              e.target.value = '';
            }}
          />
          <button className="send-btn" title="Send" onClick={submit} disabled={!draft.trim()}>
            ↵
          </button>
        </div>
      </div>
    </aside>
  );
}

// Scalar props on purpose: the state doc is patched IN PLACE client-side, so
// comparing fields of the (shared, mutated) message object would never detect
// a change. Scalars are snapshotted per render and compare correctly.
const Bubble = memo(
  ({ role, text, streaming }: { role: ChatMessage['role']; text: string; streaming: boolean }) => (
    <div className={`bubble bubble-${role}`}>
      {role === 'assistant' ? (
        <div className="bubble-md">
          <Md text={text + (streaming ? ' ▍' : '')} />
        </div>
      ) : (
        <div className="bubble-text">{text}</div>
      )}
    </div>
  ),
);
