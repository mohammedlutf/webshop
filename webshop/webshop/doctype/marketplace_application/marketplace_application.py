"""Vendor onboarding: a shopper applies, an admin reviews.

The application is the queue an admin works from. Approving it creates the
`Marketplace Shop` in one step so an approved shop can never exist without the
fields a shop needs, and so the deciding review and the shop's approval stamp
are written in the same transaction.
"""

import re

import frappe
from frappe import _
from frappe.model.document import Document

STATUS_PENDING = "Pending"
STATUS_APPROVED = "Approved"
STATUS_REJECTED = "Rejected"
STATUS_CANCELLED = "Cancelled"


class MarketplaceApplication(Document):
	def validate(self):
		self.shop_name = (self.shop_name or "").strip()
		self.phone = re.sub(r"[^\d+]", "", str(self.phone or "")).strip()

		if not self.shop_name:
			frappe.throw(_("Shop name is required."))
		if len(re.sub(r"\D", "", self.phone)) < 6:
			frappe.throw(_("A valid phone number is required."))

	def on_trash(self):
		if self.status == STATUS_PENDING:
			return
		frappe.throw(_("A reviewed application cannot be deleted. Review history must be kept."))

	def approve(self, user=None):
		"""Approve and create the shop, owned by the applicant's account."""
		if self.status != STATUS_PENDING:
			frappe.throw(_("Only a pending application can be approved."))

		if self.shop and frappe.db.exists("Marketplace Shop", self.shop):
			shop_name = self.shop
		else:
			shop = frappe.get_doc(
				{
					"doctype": "Marketplace Shop",
					"shop_name": self.shop_name,
					"vendor_user": self.applicant,
					"description": self.description,
					"status": "Approved",
				}
			)
			shop.insert(ignore_permissions=True)
			shop_name = shop.name

		self.status = STATUS_APPROVED
		self.shop = shop_name
		self.reviewed_by = user or frappe.session.user
		self.reviewed_on = frappe.utils.nowdate()
		self.save(ignore_permissions=True)
		return shop_name

	def reject(self, note=None):
		if self.status != STATUS_PENDING:
			frappe.throw(_("Only a pending application can be rejected."))
		self.status = STATUS_REJECTED
		self.review_note = (note or "")[:500]
		self.reviewed_by = frappe.session.user
		self.reviewed_on = frappe.utils.nowdate()
		self.save(ignore_permissions=True)
