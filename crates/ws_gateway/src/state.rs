//! Shared process state. Sharded (`DashMap`) so distinct chats/users never
//! contend; no `.await` is ever held across a map guard (see ADR 0033).

use std::collections::HashSet;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use dashmap::DashMap;
use linka_common::config::Config;
use linka_common::ratelimit::RateLimiter;
use redis::aio::MultiplexedConnection;
use tokio::sync::{mpsc, Notify};
use uuid::Uuid;

use crate::fanin::SubCmd;

pub type ConnId = u64;
pub type UserId = i64;
pub type ChatId = i64;

/// A message queued for a connection's writer task.
#[derive(Debug, Clone)]
pub enum ServerFrame {
    /// A JSON frame, already serialized.
    Text(String),
    /// Close the socket with this application code, then end the writer.
    Close(u16),
}

pub struct ConnHandle {
    pub user_id: UserId,
    /// UUID string — the identity used in `ws:conns` members and Python events
    /// (`force_disconnect {connection_id}` is matched against this).
    pub uuid: String,
    pub tx: mpsc::Sender<ServerFrame>,
    /// Notified to make the reader loop close this connection out-of-band
    /// (connection-cap eviction arriving over `instance_inbox`).
    pub cancel: Arc<Notify>,
}

pub struct AppState {
    pub config: Config,
    pub redis: MultiplexedConnection,
    pub limiter: RateLimiter,
    pub http: reqwest::Client,
    /// Commands to the single pub/sub task (dynamic channel SUB/UNSUB).
    pub sub_tx: mpsc::Sender<SubCmd>,
    /// Unique per process; used in every routing key. Matches Python `SERVER_ID`.
    pub server_id: String,

    pub conns: DashMap<ConnId, ConnHandle>,
    /// Local routing table: chat -> connections on this process.
    pub chat_subs: DashMap<ChatId, HashSet<ConnId>>,
    /// Multi-device: user -> their connections here.
    pub user_conns: DashMap<UserId, HashSet<ConnId>>,
    /// Presence subscribe-on-demand: target user -> local connections watching.
    pub presence_subs: DashMap<UserId, HashSet<ConnId>>,
    /// Reverse of `presence_subs`: connection -> the users it watches.
    pub presence_watches: DashMap<ConnId, HashSet<UserId>>,

    next_conn_id: AtomicU64,
}

impl AppState {
    pub fn new(
        config: Config,
        redis: MultiplexedConnection,
        http: reqwest::Client,
        sub_tx: mpsc::Sender<SubCmd>,
    ) -> Self {
        Self {
            limiter: RateLimiter::new(redis.clone()),
            config,
            redis,
            http,
            sub_tx,
            server_id: Uuid::new_v4().to_string(),
            conns: DashMap::new(),
            chat_subs: DashMap::new(),
            user_conns: DashMap::new(),
            presence_subs: DashMap::new(),
            presence_watches: DashMap::new(),
            next_conn_id: AtomicU64::new(1),
        }
    }

    pub fn alloc_conn_id(&self) -> ConnId {
        self.next_conn_id.fetch_add(1, Ordering::Relaxed)
    }

    /// Register a live connection. Returns `true` if this is the user's first
    /// connection here (caller subscribes `user_events:{uid}`).
    pub fn add_conn(&self, id: ConnId, handle: ConnHandle) -> bool {
        let user_id = handle.user_id;
        let first = {
            let mut set = self.user_conns.entry(user_id).or_default();
            let was_empty = set.is_empty();
            set.insert(id);
            was_empty
        };
        self.conns.insert(id, handle);
        first
    }

    /// Subscribe a connection to a chat's local routing set. Returns `true` on
    /// the 0→1 edge (caller registers the chat in `chat_instances`).
    pub fn add_chat_sub(&self, chat_id: ChatId, conn_id: ConnId) -> bool {
        let mut set = self.chat_subs.entry(chat_id).or_default();
        let was_empty = set.is_empty();
        set.insert(conn_id);
        was_empty
    }

    /// Register a presence watch. Returns `true` on the 0→1 edge for that
    /// target (caller subscribes `presence_events:{target}`).
    pub fn add_presence_watch(&self, conn_id: ConnId, target: UserId) -> bool {
        self.presence_watches
            .entry(conn_id)
            .or_default()
            .insert(target);
        let mut set = self.presence_subs.entry(target).or_default();
        let was_empty = set.is_empty();
        set.insert(conn_id);
        was_empty
    }

    /// Drop a presence watch. Returns `true` on the 1→0 edge (caller
    /// unsubscribes the pub/sub channel).
    pub fn remove_presence_watch(&self, conn_id: ConnId, target: UserId) -> bool {
        if let Some(mut w) = self.presence_watches.get_mut(&conn_id) {
            w.remove(&target);
        }
        let mut emptied = false;
        if let Some(mut set) = self.presence_subs.get_mut(&target) {
            set.remove(&conn_id);
            emptied = set.is_empty();
        }
        if emptied {
            self.presence_subs.remove_if(&target, |_, v| v.is_empty());
        }
        emptied
    }

    /// Senders for every local connection watching `target`'s presence.
    pub fn senders_for_presence(&self, target: UserId) -> Vec<mpsc::Sender<ServerFrame>> {
        let Some(ids) = self.presence_subs.get(&target) else {
            return Vec::new();
        };
        ids.iter()
            .filter_map(|cid| self.conns.get(cid).map(|h| h.tx.clone()))
            .collect()
    }

    /// Remove a connection from every local table (idempotent). Returns
    /// `(chats_now_empty, user_gone, presence_targets_now_empty)` so the caller
    /// can deregister routing / the user channel / presence channels.
    pub fn remove_conn(&self, id: ConnId) -> (Vec<ChatId>, bool, Vec<UserId>) {
        let mut user_gone = false;
        let Some((_, handle)) = self.conns.remove(&id) else {
            return (Vec::new(), false, Vec::new());
        };
        if let Some(mut set) = self.user_conns.get_mut(&handle.user_id) {
            set.remove(&id);
            user_gone = set.is_empty();
        }
        if user_gone {
            self.user_conns.remove_if(&handle.user_id, |_, v| v.is_empty());
        }

        let mut emptied = Vec::new();
        self.chat_subs.retain(|chat_id, set| {
            if set.remove(&id) && set.is_empty() {
                emptied.push(*chat_id);
                false
            } else {
                true
            }
        });

        // Presence watches held by this connection.
        let mut presence_emptied = Vec::new();
        if let Some((_, targets)) = self.presence_watches.remove(&id) {
            for target in targets {
                if let Some(mut set) = self.presence_subs.get_mut(&target) {
                    set.remove(&id);
                    if set.is_empty() {
                        presence_emptied.push(target);
                    }
                }
            }
            for target in &presence_emptied {
                self.presence_subs.remove_if(target, |_, v| v.is_empty());
            }
        }

        (emptied, user_gone, presence_emptied)
    }

    /// Senders for every local connection subscribed to `chat_id`. The guard is
    /// dropped before the caller `try_send`s (ADR 0033: no `.await` under a lock).
    pub fn senders_for_chat(&self, chat_id: ChatId) -> Vec<mpsc::Sender<ServerFrame>> {
        let Some(ids) = self.chat_subs.get(&chat_id) else {
            return Vec::new();
        };
        ids.iter()
            .filter_map(|cid| self.conns.get(cid).map(|h| h.tx.clone()))
            .collect()
    }

    /// Senders for every local connection of `user_id` (multi-device).
    pub fn senders_for_user(&self, user_id: UserId) -> Vec<mpsc::Sender<ServerFrame>> {
        let Some(ids) = self.user_conns.get(&user_id) else {
            return Vec::new();
        };
        ids.iter()
            .filter_map(|cid| self.conns.get(cid).map(|h| h.tx.clone()))
            .collect()
    }

    /// This user's local connection ids (for dynamic added/removed_from_chat).
    pub fn conn_ids_for_user(&self, user_id: UserId) -> Vec<ConnId> {
        self.user_conns
            .get(&user_id)
            .map(|s| s.iter().copied().collect())
            .unwrap_or_default()
    }

    /// Fire the cancel notify on the connection whose UUID matches (idempotent
    /// no-op if it's already gone).
    pub fn force_disconnect(&self, uuid: &str) {
        if let Some(h) = self.conns.iter().find(|h| h.uuid == uuid) {
            h.cancel.notify_one();
        }
    }

    /// Best-effort `PUBLISH` of a JSON event to another process's inbox
    /// (`realtime_service.publish_to_instance`).
    pub async fn publish_to_instance(&self, server_id: &str, event: &serde_json::Value) {
        let mut conn = self.redis.clone();
        let _: Result<i64, _> = redis::cmd("PUBLISH")
            .arg(linka_common::redis_keys::instance_inbox(server_id))
            .arg(event.to_string())
            .query_async(&mut conn)
            .await;
    }

    /// Replicate `realtime_service.publish_event`: inject `chat_id`, look up the
    /// serving instances, PUBLISH once to each. Used by the typing handler
    /// (Step 6); harmless before then.
    #[allow(dead_code)]
    pub async fn publish_event(&self, chat_id: ChatId, mut event: serde_json::Value) {
        event["chat_id"] = serde_json::Value::String(chat_id.to_string());
        let mut conn = self.redis.clone();
        let instances: Vec<String> = redis::cmd("SMEMBERS")
            .arg(linka_common::redis_keys::chat_instances(chat_id))
            .query_async(&mut conn)
            .await
            .unwrap_or_default();
        let payload = event.to_string();
        for sid in instances {
            let _: Result<i64, _> = redis::cmd("PUBLISH")
                .arg(linka_common::redis_keys::instance_inbox(&sid))
                .arg(&payload)
                .query_async(&mut conn)
                .await;
        }
    }
}
