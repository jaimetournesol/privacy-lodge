/* Durable task status without model turns, chat messages or speech. */
(() => {
  'use strict';
  const active = r => ['watching', 'awaiting_delivery'].includes(r.status);
  const elapsed = seconds => {
    const minutes = Math.floor(Math.max(0, seconds) / 60);
    return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
  };
  window.ConductorDelegations = class {
    constructor(root, api, visible) {
      Object.assign(this, {root, api, visible, rows: new Map(), loading: false});
      this.timer = setInterval(() => {
        if (!visible() || document.hidden) return;
        this.tick();
        if (Date.now() - (this.checked || 0) >= 20000) this.refresh();
      }, 1000);
    }
    async refresh() {
      if (this.loading) return;
      this.loading = true;
      try {
        const data = await this.api('/api/control/watches');
        const records = Object.entries(data.records || {}).filter(([, r]) => active(r));
        this.root.querySelector('[data-monitor-note]')?.remove();
        const keys = new Set(records.map(([key]) => key));
        for (const [key, row] of this.rows) if (!keys.has(key)) {row.el.remove(); this.rows.delete(key);}
        for (const [key, record] of records) {
          let row = this.rows.get(key);
          if (!row) {
            const el = document.createElement('section');
            el.style.cssText = 'border:1px solid var(--line,#394150);border-radius:8px;padding:12px;margin:8px 0;overflow-wrap:anywhere';
            el.innerHTML = '<strong></strong><p class="muted"></p><p data-task-status></p><div class="row" style="flex-wrap:wrap"><label>Summaries <select aria-label="Summary interval"><option value="0">Off</option><option value="900">Every 15 minutes</option><option value="1800">Every 30 minutes</option><option value="3600">Every hour</option></select></label><button>Stop monitoring</button></div><small>Stopping monitoring leaves the worker running.</small><p role="status" data-task-error></p>';
            this.root.appendChild(el);
            row = {el, select: el.querySelector('select'), button: el.querySelector('button')};
            this.rows.set(key, row);
            row.select.onchange = () => this.update(key, {summary_interval_seconds: Number(row.select.value)});
            row.button.onclick = () => this.update(key, {cancel: true});
          }
          row.record = record;
          row.el.querySelector('strong').textContent = `${record.node} / ${record.project}`;
          row.el.querySelector('.muted').textContent = record.brief || 'Delegated task';
          const interval = record.summary_interval_seconds || 0;
          if (![...row.select.options].some(o => Number(o.value) === interval)) row.select.add(new Option(`Every ${elapsed(interval)}`, String(interval)));
          if (!row.busy && document.activeElement !== row.select) row.select.value = String(interval);
        }
        if (!records.length) this.note('No active delegated tasks.');
        this.checked = Date.now();
        this.tick();
      } catch (error) {
        this.note('Task status unavailable; monitoring may still be running.');
      } finally {this.loading = false;}
    }
    note(text) {
      let el = this.root.querySelector('[data-monitor-note]');
      if (!el) {el = document.createElement('p'); el.dataset.monitorNote = ''; this.root.appendChild(el);}
      el.textContent = text;
    }
    tick() {
      for (const {el, record: r} of this.rows.values()) {
        const since = r.created_at || (r.monitor_version !== 2 && r.deadline ? r.deadline - 10800 : null);
        const status = r.status === 'awaiting_delivery' ? 'Result ready; waiting for Conductor' : r.monitor_state === 'unreachable' ? 'Connection unavailable; retrying' : r.task_status === 'queued' ? 'Queued' : ['delivered', 'running', 'working'].includes(r.task_status) ? 'Working' : 'Monitoring';
        el.querySelector('[data-task-status]').textContent = `${status}${since ? ' · ' + elapsed(Date.now() / 1000 - since) + ' elapsed' : ''}${r.checked_at ? ' · checked ' + elapsed(Date.now() / 1000 - r.checked_at) + ' ago' : ''}`;
      }
    }
    async update(key, body) {
      const row = this.rows.get(key);
      if (!row || row.busy) return;
      row.busy = row.select.disabled = row.button.disabled = true;
      row.el.querySelector('[data-task-error]').textContent = '';
      try {
        await this.api('/api/control/watches/' + encodeURIComponent(key), {method: 'PATCH', body});
      } catch (error) {
        row.el.querySelector('[data-task-error]').textContent = error.message || 'Could not save monitoring settings.';
      } finally {
        row.busy = row.select.disabled = row.button.disabled = false;
        await this.refresh();
      }
    }
  };
})();
