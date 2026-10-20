//! Standalone offline artifact checks. There is no publication capability.
use clap::Parser;
use orchard::{
    Address as OrchardAddress, Bundle, Note,
    bundle::{Authorized, BatchValidator, BundleVersion},
    circuit::VerifyingKey,
    keys::{FullViewingKey, PreparedIncomingViewingKey, Scope},
    note::{ExtractedNoteCommitment, NoteVersion, RandomSeed, Rho},
    note_encryption::IronwoodDomain,
    value::NoteValue,
};
use pczt::{
    Pczt,
    roles::{
        signer::Signer, spend_finalizer::SpendFinalizer, tx_extractor::TransactionExtractor,
        verifier::Verifier,
    },
};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::HashSet, fs, io::Cursor, path::PathBuf};
use zcash_keys::{
    address::Address,
    keys::{UnifiedAddressRequest, UnifiedFullViewingKey},
};
use zcash_note_encryption::{Domain, try_note_decryption, try_output_recovery_with_pkd_esk};
use zcash_primitives::transaction::{
    Transaction, TxVersion,
    fees::{FeeRule, zip317},
};
use zcash_protocol::{
    consensus::{BlockHeight, BranchId},
    constants::{V6_TX_VERSION, V6_VERSION_GROUP_ID},
    local_consensus::LocalNetwork,
    memo::MemoBytes,
    value::{BalanceError, ZatBalance},
};

type Check<T> = Result<T, String>;
const MAX_MONEY: u64 = 2_100_000_000_000_000;
/// Default padding requires two actions; qualification bounds work at eight.
const MIN_ACTIONS: usize = 2;
const MAX_ACTIONS: usize = 8;

#[derive(Parser)]
#[command(about = "Offline qualification helper; publication is permanently disabled")]
struct Args {
    #[arg(long)]
    intent: PathBuf,
    #[arg(long)]
    pczt: PathBuf,
    #[arg(long)]
    trusted_wallet: PathBuf,
    #[arg(long)]
    raw: Option<PathBuf>,
    /// Check note/payment consistency only; never verify authorization or extract.
    #[arg(long)]
    effects_only: bool,
    /// Caller-supplied observation, not an authenticated chain read.
    #[arg(long)]
    tip_height: u32,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Intent {
    network: String,
    wallet: String,
    account: String,
    recipient: Recipient,
    fee: Fee,
    expiry: Expiry,
    privacy: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Recipient {
    address: String,
    amount_zat: u64,
    memo_hex: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Fee {
    rule: String,
    max_fee_zat: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Expiry {
    min_height: u32,
    max_height: u32,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TrustedWallet {
    wallet: String,
    account: String,
    ufvk: String,
    registration_address: String,
    registration_index: u64,
    nu6_3_activation_height: u32,
}

fn need(condition: bool, message: &str) -> Check<()> {
    condition.then_some(()).ok_or_else(|| message.to_owned())
}
fn describe<E: std::fmt::Debug>(context: &str, result: Result<(), E>) -> Check<()> {
    result.map_err(|e| format!("{context}: {e:?}"))
}
fn sha(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}
fn read_json<T: serde::de::DeserializeOwned>(path: &PathBuf) -> Check<T> {
    let data = fs::read(path).map_err(|_| "cannot read input JSON file".to_owned())?;
    serde_json::from_slice(&data).map_err(|e| format!("invalid input JSON schema: {e}"))
}
fn network(activation: u32) -> LocalNetwork {
    let h = Some(BlockHeight::from_u32(activation));
    LocalNetwork {
        overwinter: h,
        sapling: h,
        blossom: h,
        heartwood: h,
        canopy: h,
        nu5: h,
        nu6: h,
        nu6_1: h,
        nu6_2: h,
        nu6_3: h,
    }
}
fn receiver(params: &LocalNetwork, encoded: &str) -> Check<OrchardAddress> {
    match Address::decode(params, encoded) {
        Some(Address::Unified(ua)) => ua
            .orchard()
            .copied()
            .ok_or_else(|| "UA lacks Orchard receiver".into()),
        _ => Err("a valid regtest unified address is required".into()),
    }
}
fn expected_memo(hex_data: &str) -> Check<[u8; 512]> {
    if hex_data.is_empty() {
        return Ok(MemoBytes::empty().into_bytes());
    }
    let data =
        hex::decode(hex_data).map_err(|_| "memo_hex is not whole hexadecimal bytes".to_owned())?;
    MemoBytes::from_bytes(&data)
        .map(|m| m.into_bytes())
        .map_err(|_| "memo exceeds 512 bytes".into())
}
fn shape(tx: &Transaction, intent: &Intent, tip: u32) -> Check<()> {
    need(
        tx.version() == TxVersion::V6 && tx.consensus_branch_id() == BranchId::Nu6_3,
        "wrong transaction version/branch",
    )?;
    need(
        tx.lock_time() == 0,
        "nonzero locktime is outside this profile",
    )?;
    let expiry = u32::from(tx.expiry_height());
    need(
        intent.expiry.min_height > 0 && intent.expiry.min_height <= intent.expiry.max_height,
        "invalid approved expiry interval",
    )?;
    need(
        expiry >= intent.expiry.min_height && expiry <= intent.expiry.max_height,
        "transaction expiry differs from policy",
    )?;
    need(
        tip.checked_add(1).is_some_and(|next| next < expiry),
        "transaction can reach expiry at next mining height",
    )?;
    need(
        tx.transparent_bundle().is_none()
            && tx.sprout_bundle().is_none()
            && tx.sapling_bundle().is_none()
            && tx.orchard_bundle().is_none(),
        "prohibited transaction pool present",
    )?;
    let b = tx.ironwood_bundle().ok_or("missing Ironwood bundle")?;
    need(
        (MIN_ACTIONS..=MAX_ACTIONS).contains(&b.actions().len())
            && b.bundle_version() == BundleVersion::ironwood_v3()
            && b.flag_byte() == 7,
        "unexpected Ironwood shape/version/flags",
    )
}
fn same_transaction(actual: &Transaction, expected: &Transaction) -> Check<()> {
    need(
        actual.version() == expected.version()
            && actual.consensus_branch_id() == expected.consensus_branch_id()
            && actual.lock_time() == expected.lock_time()
            && actual.expiry_height() == expected.expiry_height()
            && actual.txid() == expected.txid(),
        "raw transaction header/txid differs from saved PCZT",
    )?;
    let a = actual.ironwood_bundle().ok_or("raw missing Ironwood")?;
    let e = expected
        .ironwood_bundle()
        .ok_or("expected missing Ironwood")?;
    need(
        a.bundle_version() == e.bundle_version()
            && a.flag_byte() == e.flag_byte()
            && a.value_balance() == e.value_balance()
            && a.anchor() == e.anchor()
            && a.authorization().proof().as_ref() == e.authorization().proof().as_ref()
            && a.actions().len() == e.actions().len(),
        "raw bundle differs from saved PCZT",
    )?;
    for (a, e) in a.actions().iter().zip(e.actions().iter()) {
        need(
            a.cv_net().to_bytes() == e.cv_net().to_bytes()
                && a.nullifier() == e.nullifier()
                && a.rk() == e.rk()
                && a.cmx() == e.cmx()
                && a.encrypted_note().epk_bytes == e.encrypted_note().epk_bytes
                && a.encrypted_note().enc_ciphertext == e.encrypted_note().enc_ciphertext
                && a.encrypted_note().out_ciphertext == e.encrypted_note().out_ciphertext
                && <[u8; 64]>::from(a.authorization()) == <[u8; 64]>::from(e.authorization()),
            "raw action differs from saved PCZT",
        )?;
    }
    // Randomized binding signatures are independently verified below.
    Ok(())
}
fn input_checks(pczt: &Pczt, sender: &FullViewingKey) -> Check<u64> {
    let mut total = 0u64;
    let mut nonzero = 0;
    let mut nullifiers = HashSet::new();
    Verifier::new(pczt.clone())
        .with_ironwood::<String, _>(|bundle| {
            let checks = (|| -> Check<()> {
                need(
                    (MIN_ACTIONS..=MAX_ACTIONS).contains(&bundle.actions().len()),
                    "unexpected parsed action count",
                )?;
                describe(
                    "cross-address restriction",
                    bundle.verify_cross_address_restriction(),
                )?;
                for action in bundle.actions() {
                    let spend = action.spend();
                    let value = spend.value().as_ref().ok_or("missing input value")?.inner();
                    need(value <= MAX_MONEY, "input value exceeds MAX_MONEY")?;
                    need(
                        *spend.note_version() == NoteVersion::V3,
                        "wrong input note version",
                    )?;
                    need(
                        spend.fvk().is_some()
                            && spend.recipient().is_some()
                            && spend.rho().is_some()
                            && spend.rseed().is_some(),
                        "incomplete input note metadata",
                    )?;
                    need(
                        spend.dummy_sk().is_none(),
                        "dummy spending key remains after finalization",
                    )?;
                    describe("input nullifier/FVK", spend.verify_nullifier(Some(sender)))?;
                    describe(
                        "input randomized validating key",
                        spend.verify_rk(Some(sender)),
                    )?;
                    describe("action value commitment", action.verify_cv_net())?;
                    describe(
                        "output note commitment",
                        action.output().verify_note_commitment(spend),
                    )?;
                    need(
                        nullifiers.insert(spend.nullifier().to_bytes()),
                        "duplicate nullifier",
                    )?;
                    if value > 0 {
                        nonzero += 1;
                        need(
                            spend
                                .fvk()
                                .as_ref()
                                .is_some_and(|f| f.to_bytes() == sender.to_bytes()),
                            "foreign sender FVK",
                        )?;
                    }
                    total = total
                        .checked_add(value)
                        .filter(|v| *v <= MAX_MONEY)
                        .ok_or("input sum overflow")?;
                }
                need(nonzero == 1, "exactly one positive input is required")
            })();
            checks.map_err(pczt::roles::verifier::OrchardError::Custom)
        })
        .map_err(|e| format!("parsed note verification failed: {e:?}"))?;
    Ok(total)
}
fn output_note(action: &pczt::orchard::Action) -> Check<Note> {
    let out = action.output();
    let recipient = OrchardAddress::from_raw_address_bytes(
        out.recipient().as_ref().ok_or("missing output recipient")?,
    )
    .into_option()
    .ok_or("invalid output receiver")?;
    let value = out.value().ok_or("missing output value")?;
    need(value <= MAX_MONEY, "output value exceeds MAX_MONEY")?;
    let rho = Rho::from_bytes(action.spend().nullifier())
        .into_option()
        .ok_or("invalid output rho")?;
    let seed = RandomSeed::from_bytes(
        *out.rseed().as_ref().ok_or("missing output randomness")?,
        &rho,
    )
    .into_option()
    .ok_or("invalid output randomness")?;
    Note::from_parts(
        recipient,
        NoteValue::from_raw(value),
        rho,
        seed,
        NoteVersion::V3,
    )
    .into_option()
    .ok_or_else(|| "invalid output note".into())
}
struct RecoveredOutput {
    note: Note,
    memo: [u8; 512],
    internal_owned: bool,
}
fn summarize_outputs(
    outputs: Vec<RecoveredOutput>,
    balance: i64,
    expiry: u32,
    intent: &Intent,
    params: &LocalNetwork,
    sender: &FullViewingKey,
    inputs: u64,
) -> Check<Value> {
    let approved = receiver(params, &intent.recipient.address)?;
    let change_address = sender.address_at(0u32, Scope::Internal);
    need(
        approved != change_address,
        "recipient equals permitted change receiver",
    )?;
    let memo = expected_memo(&intent.recipient.memo_hex)?;
    let mut payment_count = 0;
    let mut change_count = 0;
    let mut change = 0u64;
    let mut total = 0u64;
    let no_memo = MemoBytes::empty().into_bytes();
    let actions = outputs.len();
    need(
        (MIN_ACTIONS..=MAX_ACTIONS).contains(&actions),
        "unexpected recovered output count",
    )?;
    for output in outputs {
        let value = output.note.value().inner();
        let address = output.note.recipient();
        total = total
            .checked_add(value)
            .filter(|v| *v <= MAX_MONEY)
            .ok_or("output sum overflow")?;
        if value == 0 {
            need(
                output.memo == no_memo || output.memo == [0; 512],
                "nonempty dummy memo",
            )?;
        } else if address == approved {
            payment_count += 1;
            need(
                value == intent.recipient.amount_zat,
                "payment amount mismatch",
            )?;
            need(output.memo == memo, "payment memo mismatch")?;
        } else if address == change_address {
            change_count += 1;
            need(
                output.internal_owned && output.memo == no_memo,
                "change note or memo differs from permitted internal output",
            )?;
            change = change
                .checked_add(value)
                .filter(|v| *v <= MAX_MONEY)
                .ok_or("change sum overflow")?;
        } else {
            return Err("unapproved positive output".into());
        }
    }
    need(
        payment_count == 1 && change_count < actions,
        "missing/duplicate payment or extra change output",
    )?;
    let fee = inputs
        .checked_sub(total)
        .ok_or("output total exceeds inputs")?;
    need(
        balance == i64::try_from(fee).map_err(|_| "fee overflow")?,
        "input/output sum and actual fee/value balance disagree",
    )?;
    let required = zip317::FeeRule::standard()
        .fee_required(
            params,
            BlockHeight::from_u32(expiry),
            std::iter::empty(),
            std::iter::empty(),
            0,
            0,
            0,
            actions,
        )
        .map_err(|_| "ZIP317 fee calculation failed")?;
    need(
        fee == u64::from(required) && fee <= intent.fee.max_fee_zat,
        "fee violates exact ZIP317 or approved cap",
    )?;
    Ok(
        json!({"payment_zat":intent.recipient.amount_zat,"change_zat":change,"fee_zat":fee,
        "actions":actions,"nonzero_inputs":1,"nonzero_payments":payment_count,"nonzero_change_outputs":change_count,
        "memo_bytes_compared":512,"expiry_height":expiry}),
    )
}
fn effects(
    pczt: &Pczt,
    tx: &Transaction,
    intent: &Intent,
    params: &LocalNetwork,
    sender: &FullViewingKey,
    inputs: u64,
) -> Check<Value> {
    let bundle = tx.ironwood_bundle().ok_or("missing allowed bundle")?;
    let internal = PreparedIncomingViewingKey::new(&sender.to_ivk(Scope::Internal));
    let mut outputs = Vec::new();
    for (wire, actual) in pczt
        .ironwood()
        .actions()
        .iter()
        .zip(bundle.actions().iter())
    {
        let note = output_note(wire)?;
        need(
            ExtractedNoteCommitment::from(note.commitment()) == *actual.cmx(),
            "output commitment mismatch",
        )?;
        let domain = IronwoodDomain::for_action(actual);
        let pk = IronwoodDomain::get_pk_d(&note);
        let esk = IronwoodDomain::derive_esk(&note).ok_or("missing derived output key")?;
        let (recovered, address, memo) = try_output_recovery_with_pkd_esk(&domain, pk, esk, actual)
            .ok_or("actual output ciphertext cannot be authenticated")?;
        need(
            recovered == note && address == note.recipient(),
            "recovered output differs from recorded note",
        )?;
        let internal_owned = try_note_decryption(&domain, &internal, actual).is_some_and(
            |(owned, address, owned_memo)| {
                owned == note && address == note.recipient() && owned_memo == memo
            },
        );
        outputs.push(RecoveredOutput {
            note,
            memo,
            internal_owned,
        });
    }
    let actual_fee = tx
        .fee_paid::<BalanceError, _>(|_| Ok(None))
        .map_err(|_| "invalid final transaction balance")?
        .ok_or("fee cannot be determined")?;
    need(
        i64::from(*bundle.value_balance())
            == i64::try_from(u64::from(actual_fee)).map_err(|_| "fee overflow")?,
        "final fee/balance mismatch",
    )?;
    summarize_outputs(
        outputs,
        i64::from(*bundle.value_balance()),
        u32::from(tx.expiry_height()),
        intent,
        params,
        sender,
        inputs,
    )
}
fn effects_only(
    pczt: &Pczt,
    intent: &Intent,
    params: &LocalNetwork,
    sender: &FullViewingKey,
    inputs: u64,
) -> Check<Value> {
    let internal = PreparedIncomingViewingKey::new(&sender.to_ivk(Scope::Internal));
    let mut outputs = Vec::new();
    Verifier::new(pczt.clone())
        .with_ironwood::<String, _>(|bundle| {
            let checks = (|| -> Check<()> {
                need(
                    *bundle.bundle_version() == BundleVersion::ironwood_v3()
                        && bundle.flag_byte() == 7,
                    "wrong parsed bundle version/flags",
                )?;
                for (wire, actual) in pczt.ironwood().actions().iter().zip(bundle.actions()) {
                    let note = output_note(wire)?;
                    let domain = IronwoodDomain::for_pczt_action(actual);
                    let pk = IronwoodDomain::get_pk_d(&note);
                    let esk =
                        IronwoodDomain::derive_esk(&note).ok_or("missing derived output key")?;
                    let (recovered, address, memo) =
                        try_output_recovery_with_pkd_esk(&domain, pk, esk, actual)
                            .ok_or("PCZT output ciphertext cannot be authenticated")?;
                    need(
                        recovered == note && address == note.recipient(),
                        "PCZT ciphertext/recorded note mismatch",
                    )?;
                    let internal_owned = try_note_decryption(&domain, &internal, actual)
                        .is_some_and(|(owned, address, owned_memo)| {
                            owned == note && address == note.recipient() && owned_memo == memo
                        });
                    outputs.push(RecoveredOutput {
                        note,
                        memo,
                        internal_owned,
                    });
                }
                Ok(())
            })();
            checks.map_err(pczt::roles::verifier::OrchardError::Custom)
        })
        .map_err(|e| format!("preauthorization output verification failed: {e:?}"))?;
    let (magnitude, negative) = *pczt.ironwood().value_sum();
    need(!negative, "negative Ironwood-only fee balance")?;
    summarize_outputs(
        outputs,
        i64::try_from(magnitude).map_err(|_| "balance overflow")?,
        *pczt.global().expiry_height(),
        intent,
        params,
        sender,
        inputs,
    )
}
fn run(args: Args) -> Check<Value> {
    need(
        !(args.effects_only && args.raw.is_some()),
        "effects-only mode cannot accept raw transaction bytes",
    )?;
    let intent: Intent = read_json(&args.intent)?;
    let trusted: TrustedWallet = read_json(&args.trusted_wallet)?;
    need(
        intent.network == "regtest"
            && intent.privacy == "FullPrivacy"
            && intent.fee.rule == "zip317",
        "unsupported intent policies",
    )?;
    need(
        intent.wallet == trusted.wallet && intent.account == trusted.account,
        "intent wallet/account differs from trusted bootstrap",
    )?;
    need(
        intent.recipient.amount_zat > 0
            && intent.recipient.amount_zat <= MAX_MONEY
            && intent.fee.max_fee_zat > 0
            && intent
                .recipient
                .amount_zat
                .checked_add(intent.fee.max_fee_zat)
                .is_some_and(|v| v <= MAX_MONEY),
        "invalid amount/fee bound",
    )?;
    need(
        trusted.nu6_3_activation_height > 0 && args.tip_height >= trusted.nu6_3_activation_height,
        "NU6.3 not active at supplied tip",
    )?;
    let params = network(trusted.nu6_3_activation_height);
    let ufvk = UnifiedFullViewingKey::decode(&params, trusted.ufvk.trim())
        .map_err(|_| "invalid trusted regtest UFVK")?;
    let sender = ufvk
        .orchard()
        .ok_or("trusted UFVK lacks Orchard component")?;
    let registration = receiver(&params, &trusted.registration_address)?;
    let derived = ufvk
        .address(
            trusted.registration_index.into(),
            UnifiedAddressRequest::ORCHARD,
        )
        .map_err(|_| "cannot derive trusted registration index")?;
    need(
        derived.orchard() == Some(&registration),
        "bootstrap address does not match UFVK/index",
    )?;
    let saved = fs::read(&args.pczt).map_err(|_| "cannot read PCZT file")?;
    let mut pczt = Pczt::parse(&saved).map_err(|e| format!("PCZT parsing failed: {e:?}"))?;
    let g = pczt.global();
    need(
        *g.tx_version() == V6_TX_VERSION
            && *g.version_group_id() == V6_VERSION_GROUP_ID
            && *g.consensus_branch_id() == u32::from(BranchId::Nu6_3),
        "wrong PCZT version/group/branch",
    )?;
    need(
        pczt.transparent().inputs().is_empty()
            && pczt.transparent().outputs().is_empty()
            && pczt.sapling().spends().is_empty()
            && pczt.sapling().outputs().is_empty()
            && pczt.orchard().actions().is_empty()
            && *pczt.orchard().value_sum() == (0, false)
            && *pczt.sapling().value_sum() == 0,
        "prohibited PCZT pool present",
    )?;
    need(
        (MIN_ACTIONS..=MAX_ACTIONS).contains(&pczt.ironwood().actions().len())
            && *pczt.ironwood().flags() == 7
            && pczt.ironwood().anchor().is_some(),
        "wrong Ironwood PCZT shape or missing anchor",
    )?;
    pczt.resolve_fields()
        .map_err(|e| format!("cannot resolve PCZT fields: {e:?}"))?;
    let inputs = input_checks(&pczt, sender)?;
    need(
        pczt::common::determine_lock_time(pczt.global(), pczt.transparent().inputs()) == Some(0),
        "nonzero/indeterminate PCZT locktime",
    )?;
    let expiry = *pczt.global().expiry_height();
    need(
        intent.expiry.min_height > 0
            && intent.expiry.min_height <= intent.expiry.max_height
            && expiry >= intent.expiry.min_height
            && expiry <= intent.expiry.max_height,
        "PCZT expiry outside approved interval",
    )?;
    need(
        args.tip_height
            .checked_add(1)
            .is_some_and(|next| next < expiry),
        "PCZT expiry may be reached at next mining height",
    )?;
    // Reject unapproved effects before spending time on circuit-key generation.
    // The decisive final-byte checks below are still mandatory in completed mode.
    let preliminary_effects = effects_only(&pczt, &intent, &params, sender, inputs)?;
    if args.effects_only {
        return Ok(
            json!({"helper":"offline-disabled-artifact-verifier","mode":"effects-only",
            "publication_enabled":false,"live_adapter_qualified":false,"artifact_effects_verified":true,
            "authorization_verified":false,"cryptographic_authorization_verified":false,"raw_response_verified":false,
            "consensus_txid":null,"pczt_sha256":sha(&saved),"effects":preliminary_effects,
            "chain_tip_source":"caller-supplied, not authenticated by helper"}),
        );
    }
    let sighash = Signer::new(pczt.clone())
        .map_err(|e| format!("cannot compute saved-PCZT sighash: {e:?}"))?
        .shielded_sighash();
    let vk = VerifyingKey::build(BundleVersion::ironwood_v3().circuit_version());
    let finalized = SpendFinalizer::new(pczt.clone())
        .finalize_spends()
        .map_err(|e| format!("spend finalization failed: {e:?}"))?;
    let expected = TransactionExtractor::new(finalized)
        .with_orchard(&vk)
        .extract()
        .map_err(|e| format!("independent PCZT proof/authorization extraction failed: {e:?}"))?;
    shape(&expected, &intent, args.tip_height)?;
    let mut raw_digest = None;
    let actual = if let Some(path) = &args.raw {
        let raw = fs::read(path).map_err(|_| "cannot read raw transaction file")?;
        let mut cursor = Cursor::new(raw.as_slice());
        let actual = Transaction::read(&mut cursor, BranchId::Nu6_3)
            .map_err(|_| "raw transaction parsing failed")?;
        need(
            cursor.position() == raw.len() as u64,
            "raw transaction has trailing bytes",
        )?;
        let mut canonical = Vec::new();
        actual
            .write(&mut canonical)
            .map_err(|_| "cannot serialize parsed raw transaction")?;
        need(
            canonical == raw,
            "raw transaction is not canonically encoded",
        )?;
        shape(&actual, &intent, args.tip_height)?;
        same_transaction(&actual, &expected)?;
        raw_digest = Some(sha(&raw));
        actual
    } else {
        expected
    };
    let actual_bundle: &Bundle<Authorized, ZatBalance> =
        actual.ironwood_bundle().ok_or("missing Ironwood")?;
    let mut validator = BatchValidator::new(&vk);
    describe(
        "actual authorization batch preparation",
        validator.add_bundle(actual_bundle, sighash),
    )?;
    need(
        validator.validate(rand_core::OsRng),
        "actual proof/spend/binding signature validation failed",
    )?;
    let payment_effects = effects(&pczt, &actual, &intent, &params, sender, inputs)?;
    Ok(
        json!({"helper":"offline-disabled-artifact-verifier","publication_enabled":false,
        "live_adapter_qualified":false,"artifact_effects_verified":true,"authorization_verified":true,
        "cryptographic_authorization_verified":true,
        "raw_response_verified":args.raw.is_some(),"chain_tip_source":"caller-supplied, not authenticated by helper",
        "consensus_txid":actual.txid().to_string(),"raw_sha256":raw_digest,"pczt_sha256":sha(&saved),
        "profile":"regtest-NU6.3-V6-Ironwood-up-to-8-actions-1-recipient","effects":payment_effects}),
    )
}
/// CLI entry point. This helper has no link to the service's publication gate.
pub fn main_entry() {
    match run(Args::parse()) {
        Ok(report) => println!(
            "{}",
            serde_json::to_string_pretty(&report).expect("JSON report serialization")
        ),
        Err(error) => {
            eprintln!(
                "{}",
                json!({"publication_enabled":false,"live_adapter_qualified":false,
            "artifact_effects_verified":false,"authorization_verified":false,"error":error})
            );
            std::process::exit(2);
        }
    }
}
