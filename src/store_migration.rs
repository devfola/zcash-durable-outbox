use crate::{Error, Result};
use rusqlite::Transaction;

/// Bind each consensus transaction identity to at most one business payment.
/// Run inside the same IMMEDIATE transaction as every other schema migration.
pub(crate) fn ensure_txid_uniqueness(tx: &Transaction<'_>) -> Result<()> {
    let duplicates: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM payments WHERE txid IS NOT NULL \
         GROUP BY txid COLLATE NOCASE HAVING count(*)>1)",
        [],
        |row| row.get(0),
    )?;
    if duplicates {
        return Err(Error::Reconciliation(
            "transaction ID is bound to multiple payments; preserve all rows and reconcile before migration"
                .into(),
        ));
    }
    tx.execute_batch(
        "CREATE UNIQUE INDEX IF NOT EXISTS payments_txid_unique \
         ON payments(txid COLLATE NOCASE) WHERE txid IS NOT NULL;",
    )?;
    Ok(())
}
