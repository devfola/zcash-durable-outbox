//! Durable coordination primitives. Live construction is gated on qualified adapter semantics.
pub mod engine;
pub mod intent;
pub mod rpc;
pub mod store;
mod store_migration;
pub mod zallet;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("invalid request: {0}")]
    Invalid(String),
    #[error("payment ID is already bound to a different intent")]
    Conflict,
    #[error("payment not found")]
    NotFound,
    #[error("wallet is owned by payment {0}; reconciliation may be required")]
    WalletBusy(String),
    #[error("operation {0} is uncertain; explicit reconciliation is required")]
    Reconciliation(String),
    #[error("stored transaction integrity check failed")]
    Integrity,
    #[error("live adapter is not qualified: {0}")]
    Unqualified(String),
    #[error("remote operation failed or its result is uncertain: {0}")]
    Remote(String),
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Io(#[from] std::io::Error),
}
pub type Result<T> = std::result::Result<T, Error>;

#[cfg(test)]
mod store_migration_tests;
#[cfg(test)]
mod tests;
