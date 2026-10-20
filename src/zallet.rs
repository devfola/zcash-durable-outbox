//! Pinned disposable-regtest Zallet PCZT adapter. No RPC is automatically replayed.
use crate::{
    engine::Wallet,
    intent::{digest, Intent},
    rpc::{pinned_regtest, Rpc},
    store::OperationBinding,
    Error, Result,
};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ZalletConfig {
    pub wallet_url: String,
    pub node_url: String,
    pub credential_file: PathBuf,
    pub trusted_wallet: PathBuf,
    pub verifier: PathBuf,
    pub evidence_dir: PathBuf,
}

#[derive(Deserialize)]
struct Identity {
    wallet: String,
    account: String,
    nu6_3_activation_height: u32,
}

pub struct Zallet {
    config: ZalletConfig,
    wallet: Rpc,
    node: Rpc,
    identity: Identity,
    trusted_digest: String,
    verifier_digest: String,
    directory_lock: File,
    operation_lock: Mutex<()>,
    intents: Mutex<HashMap<String, Intent>>,
}

struct DirectoryGuard<'a>(&'a File);
impl Drop for DirectoryGuard<'_> {
    fn drop(&mut self) {
        let _ = self.0.unlock();
    }
}

fn private_directory(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(path)?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    #[cfg(not(unix))]
    fs::create_dir_all(path)?;
    Ok(())
}

fn save(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    #[cfg(unix)]
    if let Some(parent) = path.parent() {
        File::open(parent)?.sync_all()?;
    }
    Ok(())
}

fn save_json(path: &Path, value: &Value) -> Result<()> {
    save(path, &serde_json::to_vec_pretty(value)?)
}

fn create_params(intent: &Intent) -> Value {
    let amount = intent.recipient.amount_zat;
    let mut recipient = json!({"address":intent.recipient.address,
        "amount":format!("{}.{:08}", amount / 100_000_000, amount % 100_000_000)});
    if !intent.recipient.memo_hex.is_empty() {
        recipient["memo"] = json!(intent.recipient.memo_hex);
    }
    json!([intent.account, [recipient], 1, "FullPrivacy", "orchard"])
}

impl Zallet {
    /// Offline construction only: opening this adapter never calls a wallet/node.
    pub fn new(mut config: ZalletConfig) -> Result<Self> {
        config.credential_file = fs::canonicalize(&config.credential_file)?;
        config.trusted_wallet = fs::canonicalize(&config.trusted_wallet)?;
        config.verifier = fs::canonicalize(&config.verifier)?;
        let credential = fs::read_to_string(&config.credential_file)?;
        if credential.len() > 4096 || !credential.trim().contains(':') {
            return Err(Error::Invalid(
                "wallet credential file must contain a local user:password credential".into(),
            ));
        }
        let trusted = fs::read(&config.trusted_wallet)?;
        let mut identity: Identity = serde_json::from_slice(&trusted)?;
        crate::intent::validate_id(&identity.wallet)?;
        identity.account = uuid::Uuid::parse_str(&identity.account)
            .map_err(|_| Error::Invalid("trusted account UUID required".into()))?
            .to_string();
        if identity.nu6_3_activation_height != 2 {
            return Err(Error::Invalid(
                "trusted wallet must use pinned NU6.3 activation height 2".into(),
            ));
        }
        let wallet = Rpc::with_timeout(
            &config.wallet_url,
            Some(&credential),
            Duration::from_secs(900),
        )?;
        let node = Rpc::new(&config.node_url, None)?;
        private_directory(&config.evidence_dir)?;
        config.evidence_dir = fs::canonicalize(&config.evidence_dir)?;
        let mut lock_options = OpenOptions::new();
        lock_options
            .read(true)
            .write(true)
            .create(true)
            .truncate(false);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            lock_options.mode(0o600);
        }
        let directory_lock = lock_options.open(config.evidence_dir.join(".adapter.lock"))?;
        let verifier_digest = digest(&fs::read(&config.verifier)?);
        Ok(Self {
            config,
            wallet,
            node,
            identity,
            trusted_digest: digest(&trusted),
            verifier_digest,
            directory_lock,
            operation_lock: Mutex::new(()),
            intents: Mutex::new(HashMap::new()),
        })
    }

    pub fn check_identity(&self, wallet: &str, account: &str) -> Result<()> {
        let account = uuid::Uuid::parse_str(account)
            .map_err(|_| Error::Invalid("account UUID required".into()))?
            .to_string();
        if wallet != self.identity.wallet || account != self.identity.account {
            return Err(Error::Invalid(
                "configured wallet/account differs from trusted viewing-key registration".into(),
            ));
        }
        Ok(())
    }

    fn lock_directory(&self) -> Result<DirectoryGuard<'_>> {
        self.directory_lock.try_lock().map_err(|_| {
            Error::Reconciliation("adapter evidence directory is owned by another process".into())
        })?;
        Ok(DirectoryGuard(&self.directory_lock))
    }

    fn unchanged_inputs(&self) -> Result<()> {
        if digest(&fs::read(&self.config.trusted_wallet)?) != self.trusted_digest
            || digest(&fs::read(&self.config.verifier)?) != self.verifier_digest
        {
            return Err(Error::Integrity);
        }
        Ok(())
    }

    fn operation(&self, stage: &str) -> Result<PathBuf> {
        let path = self
            .config
            .evidence_dir
            .join(format!("{}-{}", stage, uuid::Uuid::new_v4()));
        private_directory(&path)?;
        #[cfg(unix)]
        File::open(&self.config.evidence_dir)?.sync_all()?;
        Ok(path)
    }

    fn call(&self, path: &Path, method: &str, params: Value) -> Result<Value> {
        save_json(
            &path.join("request.json"),
            &json!({"method":method,"params":params}),
        )?;
        // Exactly one call. On any missing response, retain all evidence and the
        // Engine's durable operation fence; never infer remote cancellation.
        match self.wallet.call(method, params) {
            Ok(result) => {
                save_json(&path.join("response.json"), &result)?;
                Ok(result)
            }
            Err(error) => {
                save_json(
                    &path.join("uncertain.json"),
                    &json!({"outcome":"unknown","automatic_replay":false}),
                )?;
                Err(error)
            }
        }
    }

    fn tip(&self, path: &Path) -> Result<u32> {
        let info = self.node.call("getblockchaininfo", json!([]))?;
        let genesis = self.node.call("getblockhash", json!([0]))?;
        save_json(&path.join("node.json"), &info)?;
        save_json(&path.join("genesis.json"), &genesis)?;
        if !pinned_regtest(&info, &genesis) {
            return Err(Error::Unqualified(
                "node is outside pinned disposable NU6.3 regtest".into(),
            ));
        }
        info["blocks"]
            .as_u64()
            .and_then(|h| u32::try_from(h).ok())
            .ok_or_else(|| Error::Remote("invalid node tip height".into()))
    }

    fn remember(&self, pczt: &[u8], intent: &Intent) -> Result<()> {
        self.intents
            .lock()
            .map_err(|_| Error::Reconciliation("adapter context lock poisoned".into()))?
            .insert(digest(pczt), intent.clone());
        Ok(())
    }

    fn intent_for(&self, pczt: &[u8]) -> Result<Intent> {
        self.intents
            .lock()
            .map_err(|_| Error::Reconciliation("adapter context lock poisoned".into()))?
            .get(&digest(pczt))
            .cloned()
            .ok_or_else(|| {
                Error::Reconciliation(
                    "saved PCZT must be inspected against its durable intent before proving".into(),
                )
            })
    }

    fn verify(
        &self,
        parent: &Path,
        pczt: &[u8],
        intent: &Intent,
        raw: Option<&[u8]>,
    ) -> Result<Value> {
        self.check_identity(&intent.wallet, &intent.account)?;
        self.unchanged_inputs()?;
        let path = parent.join(format!("verification-{}", uuid::Uuid::new_v4()));
        private_directory(&path)?;
        save(&path.join("pczt.bin"), pczt)?;
        save_json(&path.join("intent.json"), &serde_json::to_value(intent)?)?;
        if let Some(raw) = raw {
            save(&path.join("raw.bin"), raw)?;
        }
        let tip = self.tip(&path)?;
        let mut command = Command::new(&self.config.verifier);
        command
            .arg("--intent")
            .arg(path.join("intent.json"))
            .arg("--pczt")
            .arg(path.join("pczt.bin"))
            .arg("--trusted-wallet")
            .arg(&self.config.trusted_wallet)
            .arg("--tip-height")
            .arg(tip.to_string());
        if raw.is_some() {
            command.arg("--raw").arg(path.join("raw.bin"));
        } else {
            command.arg("--effects-only");
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = command
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;
        let deadline = Instant::now() + Duration::from_secs(900);
        loop {
            if child.try_wait()?.is_some() {
                break;
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                return Err(Error::Unqualified(
                    "independent verifier timed out; evidence retained".into(),
                ));
            }
            thread::sleep(Duration::from_millis(20));
        }
        let output = child.wait_with_output()?;
        save(&path.join("verifier-stdout.json"), &output.stdout)?;
        save(&path.join("verifier-stderr.json"), &output.stderr)?;
        if !output.status.success() {
            return Err(Error::Unqualified(
                "independent payment/artifact verification rejected; evidence retained".into(),
            ));
        }
        let report: Value = serde_json::from_slice(&output.stdout)?;
        if report["artifact_effects_verified"] != true || report["pczt_sha256"] != digest(pczt) {
            return Err(Error::Integrity);
        }
        if let Some(raw) = raw {
            if report["authorization_verified"] != true
                || report["cryptographic_authorization_verified"] != true
                || report["raw_response_verified"] != true
                || report["raw_sha256"] != digest(raw)
            {
                return Err(Error::Integrity);
            }
        }
        Ok(report)
    }

    fn returned_pczt(&self, path: &Path, result: &Value, intent: &Intent) -> Result<Vec<u8>> {
        let encoded = result["pczt"].as_str().ok_or_else(|| {
            Error::Remote("wallet completed without a PCZT artifact; reconcile".into())
        })?;
        let pczt = STANDARD
            .decode(encoded)
            .map_err(|_| Error::Remote("invalid wallet PCZT encoding; reconcile".into()))?;
        save(&path.join("completed.pczt"), &pczt)?;
        self.verify(path, &pczt, intent, None)?;
        self.remember(&pczt, intent)?;
        Ok(pczt)
    }
}

impl Wallet for Zallet {
    fn qualified(&self) -> Result<()> {
        let _operation = self
            .operation_lock
            .lock()
            .map_err(|_| Error::Reconciliation("adapter operation lock poisoned".into()))?;
        let _directory = self.lock_directory()?;
        self.unchanged_inputs()?;
        let path = self.operation("qualification-read")?;
        self.tip(&path)?;
        let accounts_path = path.join("accounts");
        private_directory(&accounts_path)?;
        let accounts = self.call(&accounts_path, "z_listaccounts", json!([false]))?;
        if !accounts
            .as_array()
            .is_some_and(|a| a.iter().any(|a| a["account_uuid"] == self.identity.account))
        {
            return Err(Error::Unqualified(
                "trusted account is absent from the configured wallet".into(),
            ));
        }
        Ok(())
    }

    fn create(&self, intent: &Intent, binding: &OperationBinding) -> Result<Vec<u8>> {
        let _operation = self
            .operation_lock
            .lock()
            .map_err(|_| Error::Reconciliation("adapter operation lock poisoned".into()))?;
        let _directory = self.lock_directory()?;
        let intent = intent.clone().canonicalize()?;
        binding.validate(&intent)?;
        self.check_identity(&intent.wallet, &intent.account)?;
        self.unchanged_inputs()?;
        let path = self
            .config
            .evidence_dir
            .join(format!("create-{}", binding.operation_id));
        // A new operation gets a new directory. Existing unbound evidence is
        // never retroactively assigned a durable payment/owner binding.
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            fs::DirBuilder::new().mode(0o700).create(&path)?;
            File::open(&self.config.evidence_dir)?.sync_all()?;
        }
        #[cfg(not(unix))]
        fs::create_dir(&path)?;
        save_json(&path.join("binding.json"), &serde_json::to_value(binding)?)?;
        save_json(&path.join("intent.json"), &serde_json::to_value(&intent)?)?;
        self.tip(&path)?;
        let result = self.call(&path, "pczt_create", create_params(&intent))?;
        self.returned_pczt(&path, &result, &intent)
    }

    fn recover_created(
        &self,
        response: &Path,
        intent: &Intent,
        binding: &OperationBinding,
    ) -> Result<Vec<u8>> {
        let _operation = self
            .operation_lock
            .lock()
            .map_err(|_| Error::Reconciliation("adapter operation lock poisoned".into()))?;
        let _directory = self.lock_directory()?;
        let intent = intent.clone().canonicalize()?;
        binding.validate(&intent)?;
        self.check_identity(&intent.wallet, &intent.account)?;
        self.unchanged_inputs()?;

        let response = fs::canonicalize(response)?;
        let parent = response.parent().ok_or_else(|| {
            Error::Invalid("saved CREATE response has no operation directory".into())
        })?;
        let operation_name = parent
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| Error::Invalid("invalid saved CREATE operation name".into()))?;
        let operation_uuid = operation_name.strip_prefix("create-").ok_or_else(|| {
            Error::Invalid("recovery accepts only a saved CREATE operation".into())
        })?;
        if parent.parent() != Some(self.config.evidence_dir.as_path())
            || response.file_name().and_then(|name| name.to_str()) != Some("response.json")
            || operation_uuid != binding.operation_id
        {
            return Err(Error::Invalid(
                "saved CREATE evidence must belong to this adapter evidence directory".into(),
            ));
        }
        let read_source = |name: &str| -> Result<Vec<u8>> {
            let path = parent.join(name);
            // Reject sibling links escaping or redirecting the recorded operation.
            if fs::canonicalize(&path)? != path {
                return Err(Error::Integrity);
            }
            let metadata = fs::symlink_metadata(&path)?;
            if !metadata.file_type().is_file() || metadata.len() > 64 * 1024 * 1024 {
                return Err(Error::Integrity);
            }
            Ok(fs::read(path)?)
        };
        let response_bytes = read_source("response.json")?;
        let request_bytes = read_source("request.json")?;
        let intent_bytes = read_source("intent.json")?;
        let binding_bytes = read_source("binding.json")?;
        let pczt = read_source("completed.pczt")?;
        let saved_binding: OperationBinding = serde_json::from_slice(&binding_bytes)?;
        saved_binding.validate(&intent)?;
        if &saved_binding != binding {
            return Err(Error::Integrity);
        }
        let request: Value = serde_json::from_slice(&request_bytes)?;
        if request != json!({"method":"pczt_create","params":create_params(&intent)}) {
            return Err(Error::Integrity);
        }
        let saved_intent: Intent = serde_json::from_slice(&intent_bytes)?;
        if saved_intent.canonicalize()?.canonical_json()? != intent.canonical_json()? {
            return Err(Error::Integrity);
        }
        let result: Value = serde_json::from_slice(&response_bytes)?;
        if !result.is_object() || result.get("error").is_some() {
            return Err(Error::Integrity);
        }
        let encoded = result["pczt"].as_str().ok_or(Error::Integrity)?;
        let decoded = STANDARD.decode(encoded).map_err(|_| Error::Integrity)?;
        if decoded != pczt {
            return Err(Error::Integrity);
        }

        let path = self.operation("reconcile-create")?;
        save(&path.join("source-response.json"), &response_bytes)?;
        save(&path.join("source-request.json"), &request_bytes)?;
        save(&path.join("source-intent.json"), &intent_bytes)?;
        save(&path.join("source-binding.json"), &binding_bytes)?;
        save(&path.join("recovered.pczt"), &pczt)?;
        self.verify(&path, &pczt, &intent, None)?;
        let inspection_path = path.join("readonly-inspection");
        private_directory(&inspection_path)?;
        let inspection = self.call(
            &inspection_path,
            "pczt_inspect",
            json!([STANDARD.encode(&pczt)]),
        )?;
        if inspection["wallet_created"] != true {
            return Err(Error::Integrity);
        }
        // Locks prevent adapter writes, and rereading detects external source edits.
        if read_source("response.json")? != response_bytes
            || read_source("request.json")? != request_bytes
            || read_source("intent.json")? != intent_bytes
            || read_source("binding.json")? != binding_bytes
            || read_source("completed.pczt")? != pczt
        {
            return Err(Error::Integrity);
        }
        self.unchanged_inputs()?;
        save_json(
            &path.join("audit.json"),
            &json!({"operation":"recover_saved_create","source_operation":operation_name,
                "source_response_sha256":digest(&response_bytes),
                "source_request_sha256":digest(&request_bytes),
                "source_binding_sha256":digest(&binding_bytes),"operation_binding":binding,
                "source_intent_sha256":digest(&intent_bytes),"pczt_sha256":digest(&pczt),
                "canonical_intent_sha256":digest(intent.canonical_json()?.as_bytes()),
                "independent_effects_verified":true,"wallet_created_observed":true,
                "wallet_mutations":false,"automatic_remote_replay":false}),
        )?;
        self.remember(&pczt, &intent)?;
        Ok(pczt)
    }

    fn inspect(&self, pczt: &[u8], intent: &Intent) -> Result<()> {
        let _operation = self
            .operation_lock
            .lock()
            .map_err(|_| Error::Reconciliation("adapter operation lock poisoned".into()))?;
        let _directory = self.lock_directory()?;
        let path = self.operation("inspect")?;
        // RPC inspection is retained as observation; authorization comes from
        // the independent helper, which checks memo bytes and trusted change.
        let result = self.call(&path, "pczt_inspect", json!([STANDARD.encode(pczt)]))?;
        if result["wallet_created"] != true {
            return Err(Error::Integrity);
        }
        self.verify(&path, pczt, intent, None)?;
        self.remember(pczt, intent)
    }

    fn prove(&self, pczt: &[u8]) -> Result<Vec<u8>> {
        let _operation = self
            .operation_lock
            .lock()
            .map_err(|_| Error::Reconciliation("adapter operation lock poisoned".into()))?;
        let _directory = self.lock_directory()?;
        let intent = self.intent_for(pczt)?;
        let path = self.operation("prove")?;
        save(&path.join("input.pczt"), pczt)?;
        self.verify(&path, pczt, &intent, None)?;
        let result = self.call(&path, "pczt_prove", json!([STANDARD.encode(pczt)]))?;
        self.returned_pczt(&path, &result, &intent)
    }

    fn sign(&self, pczt: &[u8], intent: &Intent) -> Result<Vec<u8>> {
        let _operation = self
            .operation_lock
            .lock()
            .map_err(|_| Error::Reconciliation("adapter operation lock poisoned".into()))?;
        let _directory = self.lock_directory()?;
        let path = self.operation("sign")?;
        save(&path.join("input.pczt"), pczt)?;
        self.verify(&path, pczt, intent, None)?;
        let result = self.call(
            &path,
            "pczt_sign",
            json!([STANDARD.encode(pczt), "FullPrivacy", true]),
        )?;
        self.returned_pczt(&path, &result, intent)
    }

    fn extract_verified(&self, pczt: &[u8], intent: &Intent) -> Result<(Vec<u8>, String)> {
        let _operation = self
            .operation_lock
            .lock()
            .map_err(|_| Error::Reconciliation("adapter operation lock poisoned".into()))?;
        let _directory = self.lock_directory()?;
        let path = self.operation("extract")?;
        save(&path.join("input.pczt"), pczt)?;
        self.verify(&path, pczt, intent, None)?;
        let result = self.call(&path, "pczt_extract", json!([STANDARD.encode(pczt)]))?;
        let raw = hex::decode(result["hex"].as_str().ok_or_else(|| {
            Error::Remote("extraction response lacks raw bytes; reconcile".into())
        })?)
        .map_err(|_| {
            Error::Remote("extraction response has malformed raw bytes; reconcile".into())
        })?;
        save(&path.join("extracted.bin"), &raw)?;
        if result["stored"] != true {
            return Err(Error::Reconciliation(
                "wallet did not record extracted transaction".into(),
            ));
        }
        let report = self.verify(&path, pczt, intent, Some(&raw))?;
        let txid = report["consensus_txid"].as_str().ok_or(Error::Integrity)?;
        if txid.len() != 64 || hex::decode(txid).is_err() || result["txid"] != txid {
            return Err(Error::Integrity);
        }
        Ok((raw, txid.to_owned()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        net::TcpListener,
        sync::{
            atomic::{AtomicBool, AtomicUsize, Ordering},
            Arc,
        },
    };

    struct RpcSentinel {
        url: String,
        calls: Arc<AtomicUsize>,
        stop: Arc<AtomicBool>,
        thread: Option<thread::JoinHandle<()>>,
    }

    impl RpcSentinel {
        fn new() -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            listener.set_nonblocking(true).unwrap();
            let url = format!("http://{}", listener.local_addr().unwrap());
            let calls = Arc::new(AtomicUsize::new(0));
            let stop = Arc::new(AtomicBool::new(false));
            let observed = calls.clone();
            let stopping = stop.clone();
            let thread = thread::spawn(move || {
                while !stopping.load(Ordering::SeqCst) {
                    match listener.accept() {
                        Ok((stream, _)) => {
                            observed.fetch_add(1, Ordering::SeqCst);
                            // Immediately disconnect an accidental RPC, so a
                            // failed guard cannot turn this into a 900s wait.
                            drop(stream);
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(2));
                        }
                        Err(error) => panic!("RPC sentinel failed: {error}"),
                    }
                }
            });
            Self {
                url,
                calls,
                stop,
                thread: Some(thread),
            }
        }
    }

    impl Drop for RpcSentinel {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::SeqCst);
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
        }
    }

    fn config(root: &Path) -> ZalletConfig {
        let credential_file = root.join("credential");
        let trusted_wallet = root.join("trusted.json");
        let verifier = root.join("offline-verifier-fixture");
        fs::write(&credential_file, b"disposable:test-only").unwrap();
        fs::write(
            &trusted_wallet,
            serde_json::to_vec(&json!({"wallet":"disposable-sender",
            "account":"12345678-1234-4234-8234-123456789abc","nu6_3_activation_height":2}))
            .unwrap(),
        )
        .unwrap();
        fs::write(&verifier, b"not executed by offline constructor tests").unwrap();
        ZalletConfig {
            wallet_url: "http://127.0.0.1:1".into(),
            node_url: "http://127.0.0.1:1".into(),
            credential_file,
            trusted_wallet,
            verifier,
            evidence_dir: root.join("evidence"),
        }
    }

    #[test]
    fn offline_constructor_binds_identity_and_rejects_changed_trust_files() {
        let root = tempfile::tempdir().unwrap();
        let config = config(root.path());
        let trusted = fs::read(&config.trusted_wallet).unwrap();
        let adapter = Zallet::new(config.clone()).unwrap();
        adapter
            .check_identity("disposable-sender", "12345678-1234-4234-8234-123456789ABC")
            .unwrap();
        assert!(adapter
            .check_identity("another-wallet", "12345678-1234-4234-8234-123456789abc")
            .is_err());
        adapter.unchanged_inputs().unwrap();
        fs::write(&config.trusted_wallet, b"changed registration").unwrap();
        assert!(matches!(adapter.unchanged_inputs(), Err(Error::Integrity)));
        fs::write(&config.trusted_wallet, trusted).unwrap();
        fs::write(&config.verifier, b"changed executable").unwrap();
        assert!(matches!(adapter.unchanged_inputs(), Err(Error::Integrity)));
    }

    #[test]
    fn evidence_directory_can_reopen_but_operations_have_exclusive_locks() {
        let root = tempfile::tempdir().unwrap();
        let config = config(root.path());
        let first = Zallet::new(config.clone()).unwrap();
        let second = Zallet::new(config).unwrap();
        let held = first.lock_directory().unwrap();
        assert!(second.lock_directory().is_err());
        drop(held);
        assert!(second.lock_directory().is_ok());
    }

    fn recovery_fixture(
        root: &Path,
        rpc: &RpcSentinel,
    ) -> (Zallet, PathBuf, Intent, OperationBinding) {
        let mut config = config(root);
        config.wallet_url = rpc.url.clone();
        config.node_url = rpc.url.clone();
        let adapter = Zallet::new(config).unwrap();
        let intent: Intent = serde_json::from_value(json!({"network":"regtest",
            "wallet":"disposable-sender","account":"12345678-1234-4234-8234-123456789abc",
            "recipient":{"address":"uregtest1fixture","amount_zat":100000,"memo_hex":"00"},
            "fee":{"rule":"zip317","max_fee_zat":50000},
            "expiry":{"min_height":100,"max_height":200},"privacy":"FullPrivacy"}))
        .unwrap();
        let binding = OperationBinding {
            operation_id: uuid::Uuid::new_v4().to_string(),
            payment_id: "bounty:offline-original".into(),
            owner: uuid::Uuid::new_v4().to_string(),
            intent_digest: digest(intent.canonical_json().unwrap().as_bytes()),
        };
        let operation = adapter
            .config
            .evidence_dir
            .join(format!("create-{}", binding.operation_id));
        fs::create_dir(&operation).unwrap();
        save_json(
            &operation.join("binding.json"),
            &serde_json::to_value(&binding).unwrap(),
        )
        .unwrap();
        let pczt = b"offline provenance fixture; never sent to a verifier or wallet";
        save_json(
            &operation.join("request.json"),
            &json!({"method":"pczt_create","params":create_params(&intent)}),
        )
        .unwrap();
        save_json(
            &operation.join("intent.json"),
            &serde_json::to_value(&intent).unwrap(),
        )
        .unwrap();
        let response = operation.join("response.json");
        save_json(&response, &json!({"pczt":STANDARD.encode(pczt)})).unwrap();
        save(&operation.join("completed.pczt"), pczt).unwrap();
        (adapter, response, intent, binding)
    }

    fn assert_recovery_stopped_offline(adapter: &Zallet, rpc: &RpcSentinel) {
        assert_eq!(
            rpc.calls.load(Ordering::SeqCst),
            0,
            "provenance rejection attempted RPC"
        );
        assert!(
            fs::read_dir(&adapter.config.evidence_dir)
                .unwrap()
                .all(|entry| {
                    !entry
                        .unwrap()
                        .file_name()
                        .to_string_lossy()
                        .starts_with("reconcile-create-")
                }),
            "provenance rejection reached verification/audit operations"
        );
    }

    #[test]
    fn recover_created_rejects_wrong_recorded_request_before_rpc() {
        let root = tempfile::tempdir().unwrap();
        let rpc = RpcSentinel::new();
        let (adapter, response, intent, binding) = recovery_fixture(root.path(), &rpc);
        let mut request = json!({"method":"pczt_create","params":create_params(&intent)});
        request["params"][1][0]["amount"] = json!("0.00200000");
        fs::write(
            response.parent().unwrap().join("request.json"),
            serde_json::to_vec(&request).unwrap(),
        )
        .unwrap();
        assert!(matches!(
            adapter.recover_created(&response, &intent, &binding),
            Err(Error::Integrity)
        ));
        assert_recovery_stopped_offline(&adapter, &rpc);
    }

    #[test]
    fn recover_created_rejects_wrong_saved_intent_before_rpc() {
        let root = tempfile::tempdir().unwrap();
        let rpc = RpcSentinel::new();
        let (adapter, response, intent, binding) = recovery_fixture(root.path(), &rpc);
        let mut wrong = intent.clone();
        wrong.recipient.memo_hex = "01".into();
        fs::write(
            response.parent().unwrap().join("intent.json"),
            serde_json::to_vec(&wrong).unwrap(),
        )
        .unwrap();
        assert!(matches!(
            adapter.recover_created(&response, &intent, &binding),
            Err(Error::Integrity)
        ));
        assert_recovery_stopped_offline(&adapter, &rpc);
    }

    #[test]
    fn recover_created_rejects_response_artifact_mismatch_before_rpc() {
        let root = tempfile::tempdir().unwrap();
        let rpc = RpcSentinel::new();
        let (adapter, response, intent, binding) = recovery_fixture(root.path(), &rpc);
        fs::write(
            response.parent().unwrap().join("completed.pczt"),
            b"different saved artifact",
        )
        .unwrap();
        assert!(matches!(
            adapter.recover_created(&response, &intent, &binding),
            Err(Error::Integrity)
        ));
        assert_recovery_stopped_offline(&adapter, &rpc);
    }

    #[test]
    fn recover_created_rejects_response_outside_evidence_root_before_rpc() {
        let root = tempfile::tempdir().unwrap();
        let rpc = RpcSentinel::new();
        let (adapter, response, intent, binding) = recovery_fixture(root.path(), &rpc);
        let outside = root.path().join(format!("create-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&outside).unwrap();
        for name in [
            "response.json",
            "request.json",
            "intent.json",
            "binding.json",
            "completed.pczt",
        ] {
            fs::copy(response.parent().unwrap().join(name), outside.join(name)).unwrap();
        }
        assert!(matches!(
            adapter.recover_created(&outside.join("response.json"), &intent, &binding),
            Err(Error::Invalid(_))
        ));
        assert_recovery_stopped_offline(&adapter, &rpc);
    }

    #[test]
    fn recover_created_rejects_missing_original_binding_before_rpc() {
        let root = tempfile::tempdir().unwrap();
        let rpc = RpcSentinel::new();
        let (adapter, response, intent, binding) = recovery_fixture(root.path(), &rpc);
        fs::remove_file(response.parent().unwrap().join("binding.json")).unwrap();
        assert!(matches!(
            adapter.recover_created(&response, &intent, &binding),
            Err(Error::Io(error)) if error.kind() == std::io::ErrorKind::NotFound
        ));
        assert_recovery_stopped_offline(&adapter, &rpc);
    }

    #[test]
    fn recover_created_rejects_each_wrong_saved_binding_field_before_rpc() {
        for field in ["operation_id", "payment_id", "owner", "intent_digest"] {
            let root = tempfile::tempdir().unwrap();
            let rpc = RpcSentinel::new();
            let (adapter, response, intent, binding) = recovery_fixture(root.path(), &rpc);
            let mut wrong = binding.clone();
            match field {
                "operation_id" => wrong.operation_id = uuid::Uuid::new_v4().to_string(),
                "payment_id" => wrong.payment_id = "bounty:another-original-id".into(),
                "owner" => wrong.owner = uuid::Uuid::new_v4().to_string(),
                "intent_digest" => wrong.intent_digest = "00".repeat(32),
                _ => unreachable!(),
            }
            fs::write(
                response.parent().unwrap().join("binding.json"),
                serde_json::to_vec(&wrong).unwrap(),
            )
            .unwrap();
            assert!(
                adapter
                    .recover_created(&response, &intent, &binding)
                    .is_err(),
                "recovery accepted different {field} with an otherwise identical intent"
            );
            assert_recovery_stopped_offline(&adapter, &rpc);
        }
    }

    #[test]
    fn recover_created_rejects_different_directory_operation_before_rpc() {
        let root = tempfile::tempdir().unwrap();
        let rpc = RpcSentinel::new();
        let (adapter, response, intent, mut binding) = recovery_fixture(root.path(), &rpc);
        // Keep the source intact; the expected operation must name this exact directory.
        binding.operation_id = uuid::Uuid::new_v4().to_string();
        assert!(matches!(
            adapter.recover_created(&response, &intent, &binding),
            Err(Error::Invalid(_))
        ));
        assert_recovery_stopped_offline(&adapter, &rpc);
    }

    #[test]
    fn create_does_not_bind_or_reuse_existing_unbound_evidence() {
        let root = tempfile::tempdir().unwrap();
        let rpc = RpcSentinel::new();
        let (adapter, response, intent, binding) = recovery_fixture(root.path(), &rpc);
        let binding_path = response.parent().unwrap().join("binding.json");
        fs::remove_file(&binding_path).unwrap();
        assert!(
            matches!(adapter.create(&intent, &binding), Err(Error::Io(error))
            if error.kind() == std::io::ErrorKind::AlreadyExists)
        );
        assert!(
            !binding_path.exists(),
            "existing evidence was retroactively bound"
        );
        assert_recovery_stopped_offline(&adapter, &rpc);
    }

    #[test]
    fn production_recovery_rejects_identical_payment_a_for_b_then_accepts_original_b() {
        // Provenance-only fixture: these bytes, RPC replies and verifier report
        // are deliberately synthetic. This does not verify any Zcash artifact.
        use crate::{
            engine::{Engine, PublicationDisabled},
            store::Store,
        };
        use std::io::Read;

        struct RecoveryRpc {
            url: String,
            methods: Arc<Mutex<Vec<String>>>,
            stop: Arc<AtomicBool>,
            thread: Option<thread::JoinHandle<()>>,
        }
        impl Drop for RecoveryRpc {
            fn drop(&mut self) {
                self.stop.store(true, Ordering::SeqCst);
                if let Some(thread) = self.thread.take() {
                    let _ = thread.join();
                }
            }
        }
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let url = format!("http://{}", server.server_addr());
        let methods = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let observed = methods.clone();
        let stopping = stop.clone();
        let info: Value = serde_json::from_str(include_str!(
            "../integration/evidence/runtime-node-handoff.json"
        ))
        .unwrap();
        let info = info["result"].clone();
        let rpc = RecoveryRpc {
            url,
            methods,
            stop,
            thread: Some(thread::spawn(move || {
                while !stopping.load(Ordering::SeqCst) {
                    let Some(mut request) = server
                        .recv_timeout(Duration::from_millis(20))
                        .expect("synthetic recovery listener failed")
                    else {
                        continue;
                    };
                    let mut body = String::new();
                    request
                        .as_reader()
                        .take(1024 * 1024 + 1)
                        .read_to_string(&mut body)
                        .expect("synthetic recovery request body failed");
                    assert!(body.len() <= 1024 * 1024);
                    let body: Value = serde_json::from_str(&body).unwrap();
                    let method = body["method"].as_str().unwrap_or("").to_owned();
                    observed.lock().unwrap().push(method.clone());
                    let result = match method.as_str() {
                        "getblockchaininfo" => Some(info.clone()),
                        "getblockhash" => Some(json!(
                            "029f11d80ef9765602235e1bc9727e3eb6ba20839319f761fee920d63401e327"
                        )),
                        "pczt_inspect" => Some(json!({"wallet_created":true})),
                        _ => None,
                    };
                    let response = match result {
                        Some(result) => json!({"jsonrpc":"2.0","id":body["id"],"result":result}),
                        None => json!({"jsonrpc":"2.0","id":body["id"],
                            "error":{"code":-32601,"message":"synthetic stub forbids mutations"}}),
                    };
                    request
                        .respond(tiny_http::Response::from_string(response.to_string()))
                        .expect("synthetic recovery response failed");
                }
            })),
        };
        let root = tempfile::tempdir().unwrap();
        let pczt = b"SYNTHETIC provenance fixture; not a Zcash PCZT";
        let mut config = config(root.path());
        config.wallet_url = rpc.url.clone();
        config.node_url = rpc.url.clone();
        let report = json!({"artifact_effects_verified":true,"pczt_sha256":digest(pczt)});
        #[cfg(windows)]
        {
            config.verifier = root.path().join("synthetic-verifier.cmd");
            fs::write(&config.verifier, format!("@echo off\r\necho {report}\r\n")).unwrap();
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            config.verifier = root.path().join("synthetic-verifier.sh");
            fs::write(
                &config.verifier,
                format!("#!/bin/sh\nprintf '%s\\n' '{report}'\n"),
            )
            .unwrap();
            fs::set_permissions(&config.verifier, fs::Permissions::from_mode(0o700)).unwrap();
        }
        // Construct only after writing the executable fixture: trust hashes
        // must remain unchanged throughout the production recovery path.
        let adapter = Arc::new(Zallet::new(config).unwrap());
        let intent: Intent = serde_json::from_value(json!({"network":"regtest",
            "wallet":"disposable-sender","account":"12345678-1234-4234-8234-123456789abc",
            "recipient":{"address":"uregtest1fixture","amount_zat":100000,"memo_hex":"00"},
            "fee":{"rule":"zip317","max_fee_zat":50000},
            "expiry":{"min_height":100,"max_height":200},"privacy":"FullPrivacy"}))
        .unwrap();
        let store = Store::open(
            root.path().join("outbox.db"),
            &intent.wallet,
            &intent.account,
        )
        .unwrap();
        store.submit("A", intent.clone()).unwrap();
        store.submit("B", intent.clone()).unwrap();
        let source = |binding: &OperationBinding| {
            let directory = adapter
                .config
                .evidence_dir
                .join(format!("create-{}", binding.operation_id));
            fs::create_dir(&directory).unwrap();
            save_json(
                &directory.join("binding.json"),
                &serde_json::to_value(binding).unwrap(),
            )
            .unwrap();
            save_json(
                &directory.join("request.json"),
                &json!({"method":"pczt_create","params":create_params(&intent)}),
            )
            .unwrap();
            save_json(
                &directory.join("intent.json"),
                &serde_json::to_value(&intent).unwrap(),
            )
            .unwrap();
            save(&directory.join("completed.pczt"), pczt).unwrap();
            let response = directory.join("response.json");
            save_json(&response, &json!({"pczt":STANDARD.encode(pczt)})).unwrap();
            response
        };
        let owner_a = uuid::Uuid::new_v4().to_string();
        store
            .begin_stage("A", "pending", "create", &owner_a)
            .unwrap();
        let a = store.create_binding("A").unwrap();
        let response_a = source(&a);
        // Synthetic setup follows normal Store transitions; only a successful
        // final COMMIT releases A's wallet slot. No wallet pipeline runs here.
        store
            .finish_stage("A", "create", "created", &owner_a, pczt)
            .unwrap();
        for (expected, operation, next) in
            [("created", "prove", "proven"), ("proven", "sign", "signed")]
        {
            store
                .begin_stage("A", expected, operation, &owner_a)
                .unwrap();
            store
                .finish_stage("A", operation, next, &owner_a, pczt)
                .unwrap();
        }
        store
            .begin_stage("A", "signed", "extract", &owner_a)
            .unwrap();
        store
            .commit_final(
                "A",
                &owner_a,
                b"SYNTHETIC final bytes; never published",
                &"aa".repeat(32),
            )
            .unwrap();
        let original_a = serde_json::to_value(store.get("A").unwrap()).unwrap();
        let owner_b = uuid::Uuid::new_v4().to_string();
        store
            .begin_stage("B", "pending", "create", &owner_b)
            .unwrap();
        let b = store.create_binding("B").unwrap();
        let response_b = source(&b);
        assert_eq!(a.intent_digest, b.intent_digest);
        assert_ne!(a.operation_id, b.operation_id);
        assert_eq!(
            fs::read(&response_a).unwrap(),
            fs::read(&response_b).unwrap()
        );
        let engine = Engine::new(
            store.clone(),
            adapter.clone(),
            Arc::new(PublicationDisabled),
        );
        assert!(engine.reconcile_create("B", &response_a).is_err());
        assert!(
            rpc.methods.lock().unwrap().is_empty(),
            "cross-ID recovery reached RPC"
        );
        let rejected = store.get("B").unwrap();
        assert_eq!(rejected.owner.as_deref(), Some(owner_b.as_str()));
        assert_eq!(rejected.inflight.as_deref(), Some("create"));
        assert!(store.artifact("B", "created").is_err());
        assert_eq!(store.create_binding("B").unwrap(), b);
        let recovered = engine.reconcile_create("B", &response_b).unwrap();
        assert_eq!(recovered.stage, "created");
        assert_eq!(recovered.owner.as_deref(), Some(owner_b.as_str()));
        assert_eq!(recovered.inflight, None);
        assert_eq!(store.artifact("B", "created").unwrap(), pczt);
        assert_eq!(store.create_binding("B").unwrap(), b);
        assert_eq!(
            serde_json::to_value(store.get("A").unwrap()).unwrap(),
            original_a
        );
        let connection = store.connect().unwrap();
        assert_eq!(
            connection
                .query_row("SELECT payment FROM wallet WHERE id=1", [], |r| r
                    .get::<_, String>(0))
                .unwrap(),
            "B"
        );
        assert_eq!(
            connection
                .query_row("SELECT count(*) FROM attempts", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            *rpc.methods.lock().unwrap(),
            ["getblockchaininfo", "getblockhash", "pczt_inspect"]
        );
        adapter.unchanged_inputs().unwrap();
    }
}
