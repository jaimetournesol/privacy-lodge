import type { SurfaceComponent } from '../../shared/protocol';
import { AppPanel } from './AppPanel';
import { AudioPanel } from './AudioPanel';
import { ChartPanel } from './ChartPanel';
import { CodeBlock } from './Code';
import { Diagram } from './Diagram';
import { FileCard } from './FileCard';
import { FormPanel } from './Form';
import { HtmlPanel } from './HtmlPanel';
import { ImagePanel } from './ImagePanel';
import { Md } from './Markdown';
import { StatTiles } from './Stat';
import { TablePanel } from './Table';
import { Tasks } from './Tasks';
import { VideoPanel } from './VideoPanel';

export type EmitFn = (componentId: string, action: string, payload?: unknown) => void;

export function renderComponent(
  comp: SurfaceComponent,
  emit: EmitFn,
  reload: number,
  expanded: boolean,
): React.ReactNode {
  const p = comp.props as Record<string, any>;
  switch (comp.type) {
    case 'markdown':
      return <div className="md-card"><Md text={String(p.text ?? '')} /></div>;
    case 'code':
      return <CodeBlock code={String(p.code ?? '')} lang={p.lang} />;
    case 'table':
      return <TablePanel comp={comp} emit={emit} />;
    case 'form':
      return <FormPanel comp={comp} emit={emit} />;
    case 'tasks':
      return <Tasks items={Array.isArray(p.items) ? p.items : []} />;
    case 'stat':
      return <StatTiles items={Array.isArray(p.items) ? p.items : []} />;
    case 'chart':
      return <ChartPanel comp={comp} v={comp.v} />;
    case 'image':
      return <ImagePanel src={String(p.src ?? '')} name={p.name} />;
    case 'video':
      return <VideoPanel src={String(p.src ?? '')} />;
    case 'audio':
      return <AudioPanel src={String(p.src ?? '')} name={p.name} />;
    case 'file':
      return <FileCard src={String(p.src ?? '')} name={String(p.name ?? 'file')} size={Number(p.size ?? 0)} mime={String(p.mime ?? '')} />;
    case 'diagram':
      return <Diagram source={String(p.mermaid ?? '')} v={comp.v} />;
    case 'html':
      return <HtmlPanel comp={comp} v={comp.v} expanded={expanded} />;
    case 'app':
      return <AppPanel comp={comp} v={comp.v} reload={reload} expanded={expanded} />;
    default:
      return <pre className="mono">{JSON.stringify(comp.props, null, 2)}</pre>;
  }
}
