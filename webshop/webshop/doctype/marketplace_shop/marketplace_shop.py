"""A vendor's shop on the marketplace.

`Marketplace Shop` is the vendor entity of the platform. It is deliberately
*not* an ERPNext Supplier on its own: a Supplier carries purchasing semantics
(payables, ledgers) that a marketplace seller should only gain once the platform
actually settles with them. Instead, approving a shop *creates* the linked
Supplier (`shop.supplier`), which keeps standard ERPNext settlement documents
(Purchase Invoice, Payment Entry) available without inventing a parallel
accounting model.

Stock is intentionally NOT tracked per vendor: the platform fulfils from its own
warehouses, and the vendor association exists for attribution, commission and
payouts only.

The state transitions live on the class because that is how they are reached at
runtime: `frappe.get_doc` loads this module's class, and both the desk and the
API call `shop.approve()` / `shop.suspend(reason)` on that instance.
"""

import frappe
from frappe import _
from frappe.model.document import Document

STATUS_PENDING = "Pending"
STATUS_APPROVED = "Approved"
STATUS_SUSPENDED = "Suspended"
STATUS_REJECTED = "Rejected"


class MarketplaceShop(Document):
	def validate(self):
		self.shop_name = (self.shop_name or "").strip()
		if not self.shop_name:
			frappe.throw(_("Shop name is required."))

		# One portal account owns at most one shop. Without this a vendor could be
		# approved twice through two applications and their products/orders would
		# split across two shops with two commission rates.
		if self.vendor_user and not self.is_platform:
			owner = frappe.db.get_value(
				"Marketplace Shop",
				{"vendor_user": self.vendor_user, "name": ["!=", self.name]},
				"name",
			)
			if owner:
				frappe.throw(
					_("{0} already owns the shop `{1}`.").format(self.vendor_user, owner)
				)

		# The platform shop must never be given a commission rate: it is the
		# platform itself, and commission on yourself is a rounding error with a
		# ledger behind it.
		if self.is_platform:
			self.commission_rate = None

	def on_trash(self):
		if self.is_platform:
			frappe.throw(_("The platform shop cannot be deleted."))

		if frappe.db.exists("Website Item", {"marketplace_shop": self.name}):
			frappe.throw(
				_("Products still belong to `{0}`. Move them to another shop first.").format(
					self.shop_name
				)
			)

		if frappe.db.exists("Sales Order", {"marketplace_shop": self.name, "docstatus": 1}):
			frappe.throw(
				_("`{0}` has submitted orders and cannot be deleted.").format(self.shop_name)
			)

	# -- state transitions ------------------------------------------------

	def approve(self, user=None):
		"""Move a shop to Approved, creating its Supplier on the way."""
		if self.status == STATUS_APPROVED:
			return

		self.status = STATUS_APPROVED
		self.rejection_reason = None
		self.approved_by = user or frappe.session.user
		self.approved_on = frappe.utils.nowdate()
		if not self.is_platform and not self.supplier:
			self.supplier = ensure_supplier(self)
		self.save(ignore_permissions=True)

	def reject(self, reason=None):
		self.status = STATUS_REJECTED
		self.rejection_reason = (reason or "")[:500]
		self.save(ignore_permissions=True)

	def suspend(self, reason=None):
		if self.is_platform:
			frappe.throw(_("The platform shop cannot be suspended."))
		self.status = STATUS_SUSPENDED
		if reason:
			self.rejection_reason = (reason or "")[:500]
		self.save(ignore_permissions=True)

	def reinstate(self):
		"""Suspended -> Approved. A rejected shop must be re-reviewed explicitly."""
		if self.status == STATUS_SUSPENDED:
			self.approve()
			return
		frappe.throw(_("Only a suspended shop can be reinstated."))


def ensure_supplier(shop):
	"""Create (or reuse) the ERPNext Supplier behind a shop.

	Supplier naming differs per site (naming series, parent naming rules), so a
	clash is resolved with a numeric suffix rather than an error: the shop name
	is user-supplied and two vendors may legitimately pick the same one.

	The supplier group comes from Buying Settings, falling back to the first
	non-group Supplier Group, so this works on a site that never touched the
	Buying settings.
	"""
	supplier_name = frappe.db.get_value("Supplier", {"supplier_name": shop.shop_name}, "name")
	if supplier_name:
		return supplier_name

	group = frappe.get_cached_value("Buying Settings", None, "supplier_group")
	if not group:
		group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")

	def taken(candidate):
		return bool(frappe.db.exists("Supplier", {"supplier_name": candidate}))

	base = shop.shop_name[:140]
	candidate = base
	for suffix in range(2, 1000):
		if not taken(candidate):
			break
		candidate = "{0} ({1})".format(base[:130], suffix)

	doc = frappe.get_doc(
		{
			"doctype": "Supplier",
			"supplier_name": candidate,
			"supplier_group": group,
			"supplier_type": "Company",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def get_shop_for_user(user):
	"""The shop owned by `user`, regardless of status, or None."""
	if not user or user == "Guest":
		return None
	return frappe.db.get_value(
		"Marketplace Shop", {"vendor_user": user}, "name", ignore=True
	)
