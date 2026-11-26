"""Supplier, purchase-order, approval and field-mapping tables."""


def upgrade(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            tax_id TEXT,
            email TEXT,
            phone TEXT,
            payment_terms TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(tenant_id, name)
        );

        CREATE TABLE IF NOT EXISTS purchase_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
            po_number TEXT NOT NULL,
            supplier_name TEXT NOT NULL,
            currency TEXT,
            total TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            UNIQUE(tenant_id, po_number)
        );

        CREATE TABLE IF NOT EXISTS purchase_order_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_order_id INTEGER NOT NULL
                REFERENCES purchase_orders(id) ON DELETE CASCADE,
            idx INTEGER NOT NULL,
            description TEXT,
            quantity TEXT,
            unit_price TEXT,
            amount TEXT
        );

        CREATE TABLE IF NOT EXISTS invoice_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            invoice_id INTEGER NOT NULL UNIQUE
                REFERENCES invoices(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending_review'
                CHECK(status IN ('pending_review', 'approved', 'rejected')),
            submitted_by TEXT,
            approved_by TEXT,
            note TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mapping_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            mapping_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(tenant_id, name)
        );

        CREATE INDEX IF NOT EXISTS ix_suppliers_tenant
            ON suppliers(tenant_id, name);
        CREATE INDEX IF NOT EXISTS ix_purchase_orders_tenant
            ON purchase_orders(tenant_id, po_number);
        CREATE INDEX IF NOT EXISTS ix_approvals_tenant_status
            ON invoice_approvals(tenant_id, status);
        CREATE INDEX IF NOT EXISTS ix_mappings_tenant
            ON mapping_templates(tenant_id, name);
    """)
