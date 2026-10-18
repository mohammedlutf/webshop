"""A settlement run: what the platform owes one shop for a period.

A payout freezes the delivered orders of a period into rows, each carrying the
commission snapshot taken at *order* time, so changing a shop's commission rate
later cannot retroactively change what was already owed for past orders.

Submitting stamps the included Sales Orders with a backlink
(`Sales Order.marketplace_payout`); cancelling clears it, which is what makes a
mistaken payout recoverable without deleting history.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class MarketplacePayout(Document):
	def validate(self):
		if self.shop and frappe.db.get_value("Marketplace Shop", self.shop, "is_platform"):
			frappe.throw(_("The platform shop has no payouts: it is the platform itself."))

		if self.from_date and self.to_date and self.from_date > self.to_date:
			frappe.throw(_("The end date must be on or after the start date."))

		rebuild_rows(self)
		recompute_totals(self)

	def before_submit(self):
		if not self.orders:
			frappe.throw(_("A payout must include at least one order."))

		for row in self.orders:
			order = frappe.db.get_value(
				"Sales Order",
				row.sales_order,
				["marketplace_shop", "docstatus", "vendor_order_status", "marketplace_payout"],
				as_dict=True,
			)
			if not order:
				frappe.throw(_("Order `{0}` no longer exists.").format(row.sales_order))
			if order.marketplace_shop != self.shop:
				frappe.throw(
					_("Order `{0}` does not belong to `{1}`.").format(row.sales_order, self.shop)
				)
			if order.docstatus != 1:
				frappe.throw(_("Order `{0}` is not submitted.").format(row.sales_order))
			if order.vendor_order_status != "Delivered":
				frappe.throw(
					_("Order `{0}` has not been delivered yet and cannot be paid out.").format(
						row.sales_order
					)
				)
			if order.marketplace_payout and order.marketplace_payout != self.name:
				frappe.throw(
					_("Order `{0}` is already included in payout `{1}`.").format(
						row.sales_order, order.marketplace_payout
					)
				)

			frappe.db.set_value(
				"Sales Order", row.sales_order, "marketplace_payout", self.name, update_modified=False
			)

		self.db_set("status_display", _("Unpaid"), update_modified=False)

	def on_cancel(self):
		for row in self.orders:
			if frappe.db.get_value("Sales Order", row.sales_order, "marketplace_payout") == self.name:
				frappe.db.set_value(
					"Sales Order", row.sales_order, "marketplace_payout", None, update_modified=False
				)
		self.db_set("status_display", _("Cancelled"), update_modified=False)

	def mark_paid(self, payment_reference=None):
		"""Stamp a submitted payout as settled."""
		if self.docstatus != 1:
			frappe.throw(_("Only a submitted payout can be marked as paid."))
		self.payment_reference = (payment_reference or self.payment_reference or "")[:140]
		self.paid_on = frappe.utils.nowdate()
		self.db_set(
			{
				"payment_reference": self.payment_reference,
				"paid_on": self.paid_on,
				"status_display": _("Paid"),
			},
			update_modified=False,
		)


def eligible_orders(shop, from_date, to_date):
	"""Delivered, unpaid orders of `shop` in the period, newest first.

	The commission snapshot (`marketplace_commission_rate` / `_amount`) written
	at order time is read back here, never recomputed from the shop's *current*
	rate — that would silently re-price history.
	"""
	return frappe.get_all(
		"Sales Order",
		filters={
			"marketplace_shop": shop,
			"docstatus": 1,
			"vendor_order_status": "Delivered",
			"marketplace_payout": ["is", "not set"],
			"transaction_date": ["between", [from_date, to_date]],
		},
		fields=[
			"name",
			"transaction_date",
			"customer",
			"net_total",
			"grand_total",
			"currency",
			"marketplace_commission_rate",
			"marketplace_commission_amount",
		],
		order_by="transaction_date desc, creation desc",
		limit_page_length=0,
	)


def rebuild_rows(doc):
	"""Keep the order rows in sync with what was actually collected.

	Row values are recalculated from the stored order snapshot rather than
	trusting whatever was in the grid: an editable commission cell is a way to
	pay the platform's cut out twice.
	"""
	for row in doc.orders:
		order = frappe.db.get_value(
			"Sales Order",
			row.sales_order,
			[
				"marketplace_shop",
				"transaction_date",
				"customer",
				"net_total",
				"marketplace_commission_rate",
				"marketplace_commission_amount",
			],
			as_dict=True,
		)
		if not order:
			frappe.throw(_("Order `{0}` no longer exists.").format(row.sales_order))
		if doc.shop and order.marketplace_shop and order.marketplace_shop != doc.shop:
			frappe.throw(
				_("Order `{0}` does not belong to `{1}`.").format(row.sales_order, doc.shop)
			)

		gross = flt(order.net_total)
		commission = flt(order.marketplace_commission_amount)
		if not commission and order.marketplace_commission_rate is not None:
			commission = gross * flt(order.marketplace_commission_rate) / 100.0

		row.order_date = order.transaction_date
		row.customer = order.customer
		row.gross_amount = gross
		row.commission_rate = order.marketplace_commission_rate
		row.commission_amount = commission
		row.net_amount = gross - commission


def recompute_totals(doc):
	doc.total_gross = sum(flt(row.gross_amount) for row in doc.orders)
	doc.total_commission = sum(flt(row.commission_amount) for row in doc.orders)
	doc.total_net = sum(flt(row.net_amount) for row in doc.orders)
