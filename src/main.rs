use clap::{Parser, Subcommand};
use serde_json::{json, Value};
use std::{fs, io::Read, path::PathBuf, sync::Arc};
use zcash_durable_outbox::{
    engine::{Engine, PublicationDisabled, UnqualifiedWallet},
    intent::Intent,
    rpc::{loopback_bind, Rpc, Zebra},
    store::Store,
    zallet::{Zallet, ZalletConfig},
    Error, Result,
};

#[derive(Parser)]
#[command(
    version,
    about = "Durable single-wallet outbox; optional qualified disposable NU6.3 regtest profile"
)]
struct Args {
    #[arg(long, default_value = "runtime/outbox.db")]
    db: PathBuf,
    #[arg(long, default_value = "disposable-sender")]
    wallet: String,
    #[arg(long, default_value = "00000000-0000-0000-0000-000000000001")]
    account: String,
    #[arg(long, default_value = "http://127.0.0.1:18232")]
    zebra_url: String,
    /// Enables only the pinned disposable NU6.3 regtest adapter and verifier.
    #[arg(long)]
    live_config: Option<PathBuf>,
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    Init,
    Submit {
        id: String,
        #[arg(long)]
        intent: PathBuf,
    },
    Get {
        id: String,
    },
    Evidence {
        id: String,
    },
    Refresh {
        id: String,
        #[arg(long, default_value_t = 3)]
        confirmations: u32,
    },
    Unresolved,
    Retry {
        id: String,
    },
    /// Adopt only a qualified, already received CREATE response under its same owner.
    ReconcileCreate {
        id: String,
        #[arg(long)]
        response: PathBuf,
    },
    Serve {
        #[arg(long, default_value = "127.0.0.1:17840")]
        listen: String,
        /// Use a file with at least 32 random characters. Its contents are never printed.
        #[arg(long)]
        token_file: PathBuf,
    },
    /// Read-only getinfo call against a real disposable local Zallet process.
    ProbeWallet {
        #[arg(long, default_value = "http://127.0.0.1:28332")]
        url: String,
        #[arg(long)]
        cookie_file: PathBuf,
    },
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{}", json!({"error":error.to_string()}));
        std::process::exit(1);
    }
}
fn run() -> Result<()> {
    let args = Args::parse();
    if let Command::ProbeWallet { url, cookie_file } = &args.command {
        let credential = fs::read_to_string(cookie_file)?;
        println!("{}", Rpc::new(url, Some(&credential))?.probe()?);
        return Ok(());
    }
    if let Some(parent) = args.db.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let store = Store::open(&args.db, &args.wallet, &args.account)?;
    let enabled = args.live_config.is_some();
    let engine = if let Some(path) = &args.live_config {
        let config: ZalletConfig = serde_json::from_reader(fs::File::open(path)?)?;
        let node = Zebra {
            rpc: Rpc::new(&config.node_url, None)?,
        };
        let wallet = Zallet::new(config)?;
        wallet.check_identity(&args.wallet, &args.account)?;
        Arc::new(Engine::new(store, Arc::new(wallet), Arc::new(node)))
    } else {
        Rpc::new(&args.zebra_url, None)?;
        Arc::new(Engine::new(
            store,
            Arc::new(UnqualifiedWallet),
            Arc::new(PublicationDisabled),
        ))
    };
    match args.command {
        Command::Init => println!(
            "{}",
            json!({"database":args.db,"wallet_qualified":if enabled {Value::Null}else{json!(false)},"profile_qualified":enabled,"wallet_availability":"not_probed","publication_enabled":enabled,"profile":if enabled {"disposable-regtest-NU6.3"} else {"disabled"}})
        ),
        Command::Submit { id, intent } => {
            let intent: Intent = serde_json::from_reader(fs::File::open(intent)?)?;
            println!(
                "{}",
                serde_json::to_string(&engine.submit_or_return(&id, intent)?)?
            );
        }
        Command::Get { id } => println!("{}", serde_json::to_string(&engine.store.get(&id)?)?),
        Command::Evidence { id } => println!("{}", engine.store.evidence(&id)?),
        Command::Refresh { id, confirmations } => println!(
            "{}",
            serde_json::to_string(&engine.refresh(&id, confirmations)?)?
        ),
        Command::Unresolved => println!("{}", serde_json::to_string(&engine.store.unresolved()?)?),
        Command::Retry { id } => println!("{}", serde_json::to_string(&engine.retry(&id)?)?),
        Command::ReconcileCreate { id, response } => println!(
            "{}",
            serde_json::to_string(&engine.reconcile_create(&id, &response)?)?
        ),
        Command::Serve { listen, token_file } => serve(
            engine,
            &listen,
            fs::read_to_string(token_file)?.trim().to_owned(),
            enabled,
        )?,
        Command::ProbeWallet { .. } => unreachable!(),
    }
    Ok(())
}

fn authorized(header: &str, token: &str) -> bool {
    let expected = format!("Bearer {token}");
    let mut mismatch = header.len() ^ expected.len();
    for (a, b) in header.bytes().zip(expected.bytes()) {
        mismatch |= usize::from(a ^ b);
    }
    mismatch == 0
}

fn serve(engine: Arc<Engine>, listen: &str, token: String, enabled: bool) -> Result<()> {
    if token.len() < 32 || token.len() > 256 || !token.bytes().all(|b| b.is_ascii_graphic()) {
        return Err(Error::Invalid(
            "token must contain 32..256 printable, non-whitespace characters".into(),
        ));
    }
    let server = Arc::new(
        tiny_http::Server::http(loopback_bind(listen)?)
            .map_err(|e| Error::Io(std::io::Error::other(e.to_string())))?,
    );
    eprintln!(
        "{}",
        json!({"listen":server.server_addr().to_string(),"publication_enabled":enabled})
    );
    std::thread::scope(|scope| {
        for _ in 0..4 {
            let server = server.clone();
            let engine = engine.clone();
            let token = &token;
            scope.spawn(move || {
                for mut request in server.incoming_requests() {
                    let auth = request
                        .headers()
                        .iter()
                        .find(|h| h.field.equiv("Authorization"))
                        .map(|h| h.value.as_str())
                        .unwrap_or("");
                    let response = if !authorized(auth, token) {
                        (401, json!({"error":"unauthorized"}))
                    } else {
                        handle(&engine, &mut request, enabled)
                    };
                    let body = serde_json::to_vec(&response.1).unwrap_or_default();
                    let reply = tiny_http::Response::from_data(body)
                        .with_status_code(response.0)
                        .with_header(
                            tiny_http::Header::from_bytes("Content-Type", "application/json")
                                .unwrap(),
                        )
                        .with_header(
                            tiny_http::Header::from_bytes("Cache-Control", "no-store").unwrap(),
                        );
                    let _ = request.respond(reply);
                }
            });
        }
    });
    Ok(())
}

fn handle(engine: &Engine, request: &mut tiny_http::Request, enabled: bool) -> (u16, Value) {
    match route(engine, request, enabled) {
        Ok((status, value)) => (status, value),
        Err(error) => {
            let code = match error {
                Error::Conflict | Error::WalletBusy(_) | Error::Reconciliation(_) => 409,
                Error::NotFound => 404,
                Error::Invalid(_) | Error::Json(_) => 400,
                Error::Unqualified(_) => 503,
                Error::Remote(_) => 502,
                _ => 500,
            };
            (code, json!({"error":error.to_string()}))
        }
    }
}
fn route(engine: &Engine, request: &mut tiny_http::Request, enabled: bool) -> Result<(u16, Value)> {
    let path = request.url().to_owned();
    let method = request.method().as_str().to_owned();
    match (method.as_str(), path.as_str()) {
        ("GET", "/health") => {
            return Ok((
                200,
                json!({"wallet_qualified":if enabled {Value::Null}else{json!(false)},"profile_qualified":enabled,"wallet_availability":"not_probed","publication_enabled":enabled,"state":if enabled {"disposable_regtest"} else {"qualification_blocked"}}),
            ))
        }
        ("GET", "/payments") => {
            return Ok((200, serde_json::to_value(engine.store.unresolved()?)?))
        }
        _ => {}
    }
    let parts: Vec<_> = path.trim_start_matches('/').split('/').collect();
    if parts.len() < 2 || parts[0] != "payments" {
        return Err(Error::NotFound);
    }
    let id = parts[1];
    zcash_durable_outbox::intent::validate_id(id)?;
    match (method.as_str(), parts.len()) {
        ("GET", 2) => Ok((200, serde_json::to_value(engine.store.get(id)?)?)),
        ("PUT", 2) => {
            let mut body = Vec::new();
            request.as_reader().take(65537).read_to_end(&mut body)?;
            if body.len() > 65536 {
                return Err(Error::Invalid("request exceeds 64 KiB".into()));
            }
            let intent = serde_json::from_slice(&body)?;
            Ok((
                202,
                serde_json::to_value(engine.submit_or_return(id, intent)?)?,
            ))
        }
        ("POST", 3) if parts[2] == "retry" => Ok((200, serde_json::to_value(engine.retry(id)?)?)),
        ("GET", 3) if parts[2] == "evidence" => Ok((200, engine.store.evidence(id)?)),
        ("POST", 3) if parts[2] == "refresh" => {
            let mut body = Vec::new();
            request.as_reader().take(1025).read_to_end(&mut body)?;
            if body.len() > 1024 {
                return Err(Error::Invalid("observation request too large".into()));
            }
            #[derive(serde::Deserialize)]
            #[serde(deny_unknown_fields)]
            struct ObservationRequest {
                confirmations: u32,
            }
            let depth = if body.is_empty() {
                3
            } else {
                serde_json::from_slice::<ObservationRequest>(&body)?.confirmations
            };
            Ok((200, serde_json::to_value(engine.refresh(id, depth)?)?))
        }
        _ => Err(Error::NotFound),
    }
}

#[test]
fn bearer_auth_requires_exact_match() {
    assert!(authorized("Bearer a-secret", "a-secret"));
    assert!(!authorized("Bearer a-secret-more", "a-secret"));
    assert!(!authorized("Bearer a-secre", "a-secret"));
    assert!(!authorized("Basic a-secret", "a-secret"));
}
