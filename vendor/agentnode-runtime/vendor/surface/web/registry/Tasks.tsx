interface TaskItem {
  label: string;
  status?: 'todo' | 'doing' | 'done';
}

export function Tasks({ items }: { items: TaskItem[] }) {
  return (
    <ul className="tasks">
      {items.map((t, i) => (
        <li key={i} className={`task task-${t.status ?? 'todo'}`}>
          <span className="task-mark">
            {t.status === 'done' ? '●' : t.status === 'doing' ? '◐' : '○'}
          </span>
          <span className="task-label">{t.label}</span>
        </li>
      ))}
    </ul>
  );
}
