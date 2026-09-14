//! Owned generations: cancellation is a request; draining proves work has stopped.
use std::collections::BTreeMap;
use std::future::Future;
use std::sync::{Arc, Mutex};
use tokio::sync::{watch, Notify};

pub struct LifecycleTasks {
    generation: watch::Sender<u64>,
    active: Mutex<BTreeMap<u64, usize>>,
    changed: Notify,
}

impl Default for LifecycleTasks {
    fn default() -> Self {
        Self { generation: watch::channel(0).0, active: Mutex::new(BTreeMap::new()), changed: Notify::new() }
    }
}

impl LifecycleTasks {
    pub fn current(&self) -> u64 { *self.generation.borrow() }

    pub fn cancel(&self) -> u64 {
        // Serialize cancellation against registration: a stale child cannot be
        // added after drain has observed no work.
        let _active = self.active.lock().unwrap();
        let next = self.current() + 1;
        self.generation.send_replace(next);
        next
    }

    pub fn register(self: &Arc<Self>, generation: u64) -> Option<TaskLease> {
        let mut active = self.active.lock().unwrap();
        if self.current() != generation { return None; }
        *active.entry(generation).or_default() += 1;
        Some(TaskLease { tasks: self.clone(), generation })
    }

    pub fn spawn(self: &Arc<Self>, generation: u64, work: impl Future<Output = ()> + Send + 'static) {
        let Some(lease) = self.register(generation) else { return; };
        let mut cancelled = self.generation.subscribe();
        tauri::async_runtime::spawn(async move {
            let _lease = lease;
            let mut work = Box::pin(work);
            tokio::select! {
                biased;
                _ = cancelled.wait_for(|current| *current != generation) => {},
                _ = &mut work => {},
            }
            // Captured futures otherwise outlive local variables when this
            // task returns. Release their sockets/files before the lease can
            // announce drain completion to reset or the next generation.
            drop(work);
        });
    }

    pub async fn drain(&self) {
        loop {
            let changed = self.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            if self.active.lock().unwrap().is_empty() { return; }
            changed.await;
        }
    }
}

pub struct TaskLease { tasks: Arc<LifecycleTasks>, generation: u64 }
impl Drop for TaskLease {
    fn drop(&mut self) {
        let mut active = self.tasks.active.lock().unwrap();
        if let Some(count) = active.get_mut(&self.generation) {
            *count -= 1;
            if *count == 0 { active.remove(&self.generation); }
        }
        self.tasks.changed.notify_waiters();
    }
}

/// Database shutdown is unbounded deliberately: a migration must reach its own
/// safe boundary. Other sidecars get a grace period before forced termination.
pub async fn stop_child(child: &mut tokio::process::Child, database: bool) -> std::io::Result<()> {
    if child.try_wait()?.is_some() { return Ok(()); }
    #[cfg(unix)]
    if let Some(pid) = child.id() {
        // Child is still owned/unreaped, so its pid cannot have been reused.
        let status = tokio::process::Command::new("kill")
            .args(["-TERM", &pid.to_string()]).status().await?;
        if !status.success() && child.try_wait()?.is_none() {
            return Err(std::io::Error::other("Could not request child shutdown"));
        }
    }
    if database {
        child.wait().await?;
    } else {
        match tokio::time::timeout(std::time::Duration::from_secs(15), child.wait()).await {
            Ok(result) => { result?; },
            Err(_) => { child.kill().await?; },
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[tokio::test]
    async fn cancellation_does_not_count_as_child_exit() {
        let tasks = Arc::new(LifecycleTasks::default());
        let child = tasks.register(tasks.current()).unwrap();
        let old = tasks.current();
        tasks.cancel();
        assert!(tasks.register(old).is_none());
        assert!(tokio::time::timeout(Duration::from_millis(30), tasks.drain()).await.is_err());
        drop(child);
        tokio::time::timeout(Duration::from_secs(1), tasks.drain()).await.unwrap();
    }

    #[tokio::test]
    async fn cancelled_background_work_drops_its_resources_before_drain_returns() {
        let tasks = Arc::new(LifecycleTasks::default());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tasks.spawn(tasks.current(), async move {
            let _listener = listener;
            std::future::pending::<()>().await;
        });
        tasks.cancel();
        tokio::time::timeout(Duration::from_secs(2), tasks.drain()).await.unwrap();
        tokio::net::TcpListener::bind(address).await.unwrap();
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn database_gets_time_to_flush_after_term() {
        use tokio::io::{AsyncBufReadExt, BufReader};
        let mut child = tokio::process::Command::new("sh")
            .args(["-c", "trap 'sleep 0.3; echo flushed; exit 0' TERM; echo ready; while :; do sleep 0.05; done"])
            .stdout(std::process::Stdio::piped()).kill_on_drop(true).spawn().unwrap();
        let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
        assert_eq!(lines.next_line().await.unwrap().as_deref(), Some("ready"));
        tokio::time::timeout(Duration::from_secs(5), stop_child(&mut child, true)).await.unwrap().unwrap();
        assert!(child.try_wait().unwrap().unwrap().success());
        assert_eq!(lines.next_line().await.unwrap().as_deref(), Some("flushed"));
    }
}
