use axum::{body::Bytes, extract::{Path, State, Extension}, http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response}, routing::{get, post, put}, Json, Router};
use aws_sdk_dynamodb::types::AttributeValue as AV;
use garde::Validate;
use lambda_http::{service_fn, Body, RequestExt};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{collections::HashMap, sync::{Arc, Mutex, OnceLock}, time::Instant};
use tower::ServiceExt;

#[derive(Debug, Deserialize, Serialize, Validate)]
#[serde(deny_unknown_fields)]
struct User {
    #[garde(length(chars, min = 2, max = 100))]
    name: String,
    #[garde(custom(crate::email))]
    email: String,
    #[garde(range(min = 18, max = 120))]
    age: i64,
    #[serde(default)]
    #[garde(length(max = 10))]
    tags: Vec<String>,
}

fn email(value: &str, _: &()) -> garde::Result {
    static PATTERN: OnceLock<regex::Regex> = OnceLock::new();
    let pattern = PATTERN.get_or_init(|| regex::Regex::new(
        r"\A[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,63}\z"
    ).unwrap());
    if value.len() <= 254 && value.split('@').next().unwrap_or("").len() <= 64 && pattern.is_match(value) {
        Ok(())
    } else { Err(garde::Error::new("invalid email")) }
}

fn parse(body: &[u8]) -> Result<User, Response> {
    serde_json::from_slice::<User>(body).ok().filter(|u| u.validate().is_ok())
        .ok_or_else(|| (StatusCode::UNPROCESSABLE_ENTITY, Json(json!({"error":"validation_failed"}))).into_response())
}

#[derive(Clone)]
struct AppState {
    dynamodb: aws_sdk_dynamodb::Client,
    s3: aws_sdk_s3::Client,
    table: String,
    bucket: String,
    // Lambda runs one invocation at a time in a standard execution environment.
    used: Arc<Mutex<HashMap<&'static str, bool>>>,
}

#[derive(Clone, Default, Serialize)]
struct Meta {
    request_id: String,
    environment_id: String,
    invocation_counter: u64,
    first_invocation: bool,
    endpoint: String,
    version: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    service: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    service_first_use: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    sdk_ms: Option<f64>,
}
type Invocation = Arc<Mutex<Meta>>;

async fn health() -> Json<serde_json::Value> { Json(json!({"status":"ok"})) }
async fn validate(body: Bytes) -> Response {
    match parse(&body) { Ok(user) => Json(user).into_response(), Err(error) => error }
}

fn begin(state: &AppState, meta: &Invocation, service: &'static str) -> Instant {
    let first = !state.used.lock().unwrap().insert(service, true).unwrap_or(false);
    let mut value = meta.lock().unwrap();
    value.service = Some(service);
    value.service_first_use = Some(first);
    Instant::now()
}
fn finish(meta: &Invocation, start: Instant) {
    meta.lock().unwrap().sdk_ms = Some(start.elapsed().as_secs_f64() * 1000.0);
}
fn internal(error: impl std::fmt::Debug) -> Response {
    tracing::error!(?error, "unexpected_backend_error");
    (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error":"internal_error"}))).into_response()
}

async fn users(State(state): State<AppState>, Extension(meta): Extension<Invocation>, body: Bytes) -> Response {
    let user = match parse(&body) { Ok(user) => user, Err(error) => return error };
    let id = uuid::Uuid::new_v4().to_string();
    let item = HashMap::from([
        ("id".into(), AV::S(id.clone())), ("name".into(), AV::S(user.name.clone())),
        ("email".into(), AV::S(user.email.clone())), ("age".into(), AV::N(user.age.to_string())),
        ("tags".into(), AV::L(user.tags.iter().map(|s| AV::S(s.clone())).collect())),
    ]);
    let operation = state.dynamodb.put_item().table_name(&state.table).set_item(Some(item))
        .condition_expression("attribute_not_exists(id)");
    let start = begin(&state, &meta, "dynamodb");
    let result = operation.send().await;
    finish(&meta, start);
    match result {
        Err(error) => internal(error),
        Ok(_) => (StatusCode::CREATED, Json(json!({"id":id, "name":user.name,
            "email":user.email, "age":user.age, "tags":user.tags}))).into_response(),
    }
}

async fn files(State(state): State<AppState>, Extension(meta): Extension<Invocation>,
    Path(key): Path<String>, headers: HeaderMap, body: Bytes) -> Response {
    let content_type = headers.get("content-type").and_then(|h| h.to_str().ok()).unwrap_or("application/octet-stream");
    let operation = state.s3.put_object().bucket(&state.bucket).key(key)
        .content_type(content_type).body(body.to_vec().into());
    let start = begin(&state, &meta, "s3");
    let result = operation.send().await;
    finish(&meta, start);
    match result { Ok(_) => StatusCode::NO_CONTENT.into_response(), Err(error) => internal(error) }
}

fn router(state: AppState) -> Router {
    Router::new().route("/health", get(health)).route("/validate", post(validate))
        .route("/users", post(users)).route("/files/{*key}", put(files))
        .layer(axum::extract::DefaultBodyLimit::disable()).with_state(state)
}

#[tokio::main]
async fn main() -> Result<(), lambda_http::Error> {
    tracing_subscriber::fmt().json().with_current_span(false).with_span_list(false).init();
    // Client initialization only; no storage calls during startup.
    let config = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .retry_config(aws_config::retry::RetryConfig::standard().with_max_attempts(3))
        .timeout_config(aws_config::timeout::TimeoutConfig::builder()
            .connect_timeout(std::time::Duration::from_secs(3))
            .read_timeout(std::time::Duration::from_secs(5)).build())
        .load().await;
    let state = AppState { dynamodb: aws_sdk_dynamodb::Client::new(&config),
        s3: aws_sdk_s3::Client::new(&config), table: std::env::var("USERS_TABLE")?,
        bucket: std::env::var("FILES_BUCKET")?, used: Default::default() };
    // Compile common email grammar at startup, just as Python does at import.
    let _ = email("alice@example.com", &());
    let app = router(state);
    let environment_id = uuid::Uuid::new_v4().to_string();
    let counter = Arc::new(std::sync::atomic::AtomicU64::new(0));
    lambda_http::run(service_fn(move |request: lambda_http::Request| {
        let app = app.clone();
        let n = counter.fetch_add(1, std::sync::atomic::Ordering::Relaxed) + 1;
        let meta = Arc::new(Mutex::new(Meta { request_id: request.lambda_context().request_id,
            environment_id: environment_id.clone(), invocation_counter: n,
            first_invocation: n == 1, endpoint: request.uri().path().to_string(), ..Default::default() }));
        meta.lock().unwrap().version = std::env::var("AWS_LAMBDA_FUNCTION_VERSION").unwrap_or_else(|_| "$LATEST".into());
        async move {
            let request = request.map(|body| axum::body::Body::from(body.to_vec()));
            let mut request = request;
            request.extensions_mut().insert(meta.clone());
            let response = app.oneshot(request).await.unwrap();
            let mut value = serde_json::to_value(&*meta.lock().unwrap()).unwrap();
            value["type"] = json!("benchmark");
            value["status"] = json!(response.status().as_u16());
            println!("{value}");
            let (mut parts, body) = response.into_parts();
            let meta = meta.lock().unwrap().clone();
            for (name, value) in [("x-benchmark-request-id", meta.request_id),
                ("x-benchmark-environment-id", meta.environment_id),
                ("x-benchmark-version", meta.version),
                ("x-benchmark-invocation", meta.invocation_counter.to_string())] {
                parts.headers.insert(name, value.parse().unwrap());
            }
            if let Some(first) = meta.service_first_use {
                parts.headers.insert("x-benchmark-service-first-use", first.to_string().parse().unwrap());
            }
            if let Some(ms) = meta.sdk_ms {
                parts.headers.insert("x-benchmark-sdk-ms", ms.to_string().parse().unwrap());
            }
            let bytes = axum::body::to_bytes(body, usize::MAX).await?;
            Ok::<_, lambda_http::Error>(lambda_http::Response::from_parts(parts, Body::Binary(bytes.to_vec())))
        }
    })).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_validation_fixtures() {
        let fixtures: serde_json::Value = serde_json::from_str(include_str!("../../../fixtures/validation.json")).unwrap();
        for case in fixtures.as_array().unwrap() {
            let bytes = case.get("raw").map(|v| v.as_str().unwrap().as_bytes().to_vec())
                .unwrap_or_else(|| serde_json::to_vec(&case["input"]).unwrap());
            assert_eq!(parse(&bytes).is_ok(), case["valid"].as_bool().unwrap(), "{}", case["name"]);
        }
    }

    #[tokio::test]
    async fn framework_routes_without_aws_calls() {
        let config = aws_config::defaults(aws_config::BehaviorVersion::latest())
            .region(aws_config::Region::new("eu-north-1"))
            .credentials_provider(aws_sdk_s3::config::Credentials::new("test", "test", None, None, "test"))
            .load().await;
        let app = router(AppState { dynamodb: aws_sdk_dynamodb::Client::new(&config),
            s3: aws_sdk_s3::Client::new(&config), table: "test".into(), bucket: "test".into(), used: Default::default() });
        let response = app.clone().oneshot(axum::http::Request::builder().uri("/health")
            .body(axum::body::Body::empty()).unwrap()).await.unwrap();
        assert_eq!(response.status(), 200);
        let response = app.oneshot(axum::http::Request::builder().uri("/validate").method("POST")
            .body(axum::body::Body::from("{}" )).unwrap()).await.unwrap();
        assert_eq!(response.status(), 422);
    }
}
