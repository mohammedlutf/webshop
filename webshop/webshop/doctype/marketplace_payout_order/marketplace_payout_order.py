"""Child table of Marketplace Payout: one row per Sales Order in the run.

The numbers are stored, not derived, so a payout remains an auditable record of
what was agreed at the time even if the Sales Order changes afterwards (a rate
edit, a cancellation, a rename).
"""

from frappe.model.document import Document


class MarketplacePayoutOrder(Document):
	pass
