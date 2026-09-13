import { memo, useEffect, useRef } from 'react';
import type { SurfaceComponent } from '../../shared/protocol';

const PALETTE = ['#38e1ff', '#3ddc97', '#f5c96b', '#ff5d6c', '#b28bff', '#ff9f6b', '#6b9fff'];

let libPromise: Promise<any> | null = null;
async function loadChartJs() {
  libPromise ??= import('chart.js/auto').then((m) => {
    const Chart = m.default;
    Chart.defaults.color = '#8b8b98';
    Chart.defaults.borderColor = '#26262e';
    Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
    Chart.defaults.font.size = 11;
    return Chart;
  });
  return libPromise;
}

interface Series {
  name?: string;
  data: number[];
  color?: string;
}

const alpha = (hex: string, a: string) => hex + a;

export const ChartPanel = memo(
  ({ comp }: { comp: SurfaceComponent; v: number }) => {
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const chartRef = useRef<any>(null);
    const p = comp.props as {
      kind?: string;
      labels?: string[];
      series?: Series[];
      stacked?: boolean;
      unit?: string;
      height?: number;
    };

    useEffect(() => {
      let alive = true;
      void loadChartJs().then((Chart) => {
        if (!alive || !canvasRef.current) return;
        chartRef.current?.destroy();

        const kind = p.kind ?? 'line';
        const series = p.series ?? [];
        const labels = p.labels ?? series[0]?.data.map((_, i) => String(i + 1)) ?? [];
        const type = kind === 'area' ? 'line' : kind === 'pie' ? 'doughnut' : kind;

        const datasets =
          kind === 'pie'
            ? [
                {
                  data: series[0]?.data ?? [],
                  backgroundColor: (series[0]?.data ?? []).map((_, i) => alpha(PALETTE[i % PALETTE.length], 'cc')),
                  borderColor: '#111116',
                  borderWidth: 2,
                },
              ]
            : series.map((s, i) => {
                const color = s.color ?? PALETTE[i % PALETTE.length];
                return {
                  label: s.name ?? `series ${i + 1}`,
                  data: kind === 'scatter' ? s.data.map((y, j) => ({ x: j, y })) : s.data,
                  borderColor: color,
                  backgroundColor: kind === 'bar' ? alpha(color, 'b3') : alpha(color, '26'),
                  fill: kind === 'area',
                  tension: 0.35,
                  borderWidth: 2,
                  pointRadius: kind === 'scatter' ? 3.5 : 0,
                  pointHoverRadius: 4,
                };
              });

        chartRef.current = new Chart(canvasRef.current, {
          type: type as never,
          data: { labels, datasets },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 350 },
            plugins: {
              legend: {
                display: kind === 'pie' || series.length > 1,
                position: 'bottom',
                labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true },
              },
              tooltip: {
                backgroundColor: '#17171c',
                borderColor: '#26262e',
                borderWidth: 1,
                titleColor: '#e8e8ee',
                bodyColor: '#8b8b98',
              },
            },
            scales:
              kind === 'pie'
                ? undefined
                : {
                    x: { grid: { color: '#1c1c23' }, stacked: p.stacked },
                    y: {
                      grid: { color: '#1c1c23' },
                      stacked: p.stacked,
                      ticks: { callback: (v: unknown) => `${v}${p.unit ?? ''}` },
                    },
                  },
          },
        });
      });
      return () => {
        alive = false;
      };
    }, [comp.v]); // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => () => chartRef.current?.destroy(), []);

    return (
      <div className="chart-wrap" style={{ height: p.height ?? 280 }}>
        <canvas ref={canvasRef} />
      </div>
    );
  },
  (a, b) => a.v === b.v,
);
