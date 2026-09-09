use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tonic::{transport::Server, Request, Response, Status};

// Node ID is read from the NODE_ID env var at startup and must be unique across
// every running replica of this service, or generated IDs can collide.
fn node_id_from_env() -> Result<u64, String> {
    let raw = std::env::var("NODE_ID")
        .map_err(|_| "NODE_ID env var is required (0..=1023)".to_string())?;
    let node_id: u64 = raw
        .trim()
        .parse()
        .map_err(|_| format!("NODE_ID must be an integer, got {raw:?}"))?;
    if node_id > MAX_NODE_ID {
        return Err(format!(
            "NODE_ID must be between 0 and {MAX_NODE_ID}, got {node_id}"
        ));
    }
    Ok(node_id)
}

// 1. Import the generated Protobuf code
pub mod snowflake {
    tonic::include_proto!("snowflake");
}
use snowflake::snowflake_service_server::{SnowflakeService, SnowflakeServiceServer};
use snowflake::{NextIdRequest, NextIdResponse};

// Constants defining the ID bit structure
const EPOCH: u64 = 1704067200000; // Jan 1, 2024
const NODE_ID_BITS: u64 = 10;
const SEQUENCE_BITS: u64 = 12;

const MAX_NODE_ID: u64 = (1 << NODE_ID_BITS) - 1;
const MAX_SEQUENCE: u64 = (1 << SEQUENCE_BITS) - 1;

const NODE_ID_SHIFT: u64 = SEQUENCE_BITS;
const TIMESTAMP_SHIFT: u64 = SEQUENCE_BITS + NODE_ID_BITS;

pub struct SnowflakeGenerator {
    node_id: u64,
    // A single atomic variable holding both timestamp and sequence
    // to prevent race conditions without using a Mutex
    state: AtomicU64,
}

impl SnowflakeGenerator {
    pub fn new(node_id: u64) -> Self {
        if node_id > MAX_NODE_ID {
            panic!("Node ID must be between 0 and {}", MAX_NODE_ID);
        }
        SnowflakeGenerator {
            node_id,
            state: AtomicU64::new(0),
        }
    }

    fn current_time_ms(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("Time went backwards")
            .as_millis() as u64
    }

    pub fn next_id(&self) -> u64 {
        let mut current_state = self.state.load(Ordering::Acquire);

        loop {
            // Extract current timestamp and sequence from the state
            let last_timestamp = current_state >> SEQUENCE_BITS;
            let mut sequence = current_state & MAX_SEQUENCE;
            let mut current_timestamp = self.current_time_ms();

            // 1. Clock drift protection (backward jump)
            if current_timestamp < last_timestamp {
                std::thread::yield_now(); // Yield CPU to other threads
                continue; // Retry until the clock catches up
            }

            // 2. Handle multiple requests in the same millisecond
            if current_timestamp == last_timestamp {
                sequence = (sequence + 1) & MAX_SEQUENCE;
                if sequence == 0 {
                    // Exhausted all 4,096 IDs for this millisecond!
                    // Actively wait for the next millisecond
                    while current_timestamp <= last_timestamp {
                        current_timestamp = self.current_time_ms();
                    }
                }
            } else {
                // New millisecond - reset the sequence counter
                sequence = 0;
            }

            // Construct the new state (timestamp + sequence)
            let new_state = (current_timestamp << SEQUENCE_BITS) | sequence;

            // 3. The magic: Compare And Swap (CAS)
            match self.state.compare_exchange_weak(
                current_state,
                new_state,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => {
                    // Successfully updated memory before any other thread.
                    // Construct the final 64-bit ID and return it.
                    let timestamp_offset = current_timestamp - EPOCH;
                    return (timestamp_offset << TIMESTAMP_SHIFT)
                        | (self.node_id << NODE_ID_SHIFT)
                        | sequence;
                }
                Err(actual_state) => {
                    // Another thread beat us to it.
                    // Update our local state and loop again.
                    current_state = actual_state;
                }
            }
        }
    }
}

// 2. Define the gRPC Service Wrapper
pub struct MySnowflakeService {
    // Arc (Atomic Reference Count) allows multiple threads to safely share the generator
    generator: Arc<SnowflakeGenerator>,
}

#[tonic::async_trait]
impl SnowflakeService for MySnowflakeService {
    async fn next_id(
        &self,
        _request: Request<NextIdRequest>,
    ) -> Result<Response<NextIdResponse>, Status> {
        // Generate the ID and convert it to a String for the JSON/gRPC response
        let id = self.generator.next_id();
        Ok(Response::new(NextIdResponse { id: id.to_string() }))
    }
}

// 3. The Async Main Function
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // 0.0.0.0 is used instead of 127.0.0.1 so it can be reached from other Docker containers
    let addr = "0.0.0.0:50051".parse()?;

    // Node ID comes from the environment; a missing or out-of-range value is fatal.
    let node_id = match node_id_from_env() {
        Ok(id) => id,
        Err(e) => {
            eprintln!("fatal: {e}");
            std::process::exit(1);
        }
    };

    let generator = Arc::new(SnowflakeGenerator::new(node_id));
    let service = MySnowflakeService { generator };

    // Standard gRPC health service; orchestrators/LBs probe grpc.health.v1.Health.
    let (mut health_reporter, health_service) = tonic_health::server::health_reporter();
    health_reporter
        .set_serving::<SnowflakeServiceServer<MySnowflakeService>>()
        .await;

    println!("Snowflake gRPC Server listening on {addr} (node_id={node_id})");

    Server::builder()
        .add_service(health_service)
        .add_service(SnowflakeServiceServer::new(service))
        .serve(addr)
        .await?;

    Ok(())
}
