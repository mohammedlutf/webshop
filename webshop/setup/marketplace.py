"""Schema bootstrap for the multi-vendor marketplace.

Runs on every `bench migrate`, so a new site — or an existing one upgrading to
the marketplace architecture — arrives with the custom fields, the vendor role
and the platform shop already in place.

Custom fields are used (rather than editing core doctype JSONs) because
`Website Item` and `Sales Order` belong to ERPNext/webshop core: the fields are
ours, the doctypes are not. Every read of these fields in the API layer is
schema-tolerant (`meta.has_field(...)`), so a site that has not migrated yet
degrades to single-shop behaviour instead of raising.
"""

import frappe

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

VENDOR_ROLE = "Marketplace Vendor"
PLATFORM_SHOP_NAME = "Platform"

CUSTOM_FIELDS = {
	"Website Item": [
		{
			"fieldname": "marketplace_shop",
			"label": "Marketplace Shop",
			"fieldtype": "Link",
			"options": "Marketplace Shop",
			"insert_after": "published",
			"description": "The vendor shop this product belongs to. Blank = platform shop.",
			"index": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "vendor_ranking",
			"label": "Vendor Ranking",
			"fieldtype": "Int",
			"insert_after": "marketplace_shop",
			"default": "0",
			"description": (
				"Ordering set by the owning vendor. Applied inside that shop's listings only, "
				"never to the catalogue as a whole — a vendor must not be able to reorder the "
				"platform's storefront. The merchant's site-wide `ranking` is untouched by it."
			),
			"no_copy": 1,
		},
	],
	"Sales Order": [
		{
			"fieldname": "marketplace_order_group",
			"label": "Marketplace Order Group",
			"fieldtype": "Data",
			"insert_after": "delivery_date",
			"description": "Shared id of the customer checkout: one per vendor Sales Order of the same cart.",
			"no_copy": 1,
			"read_only": 1,
			"index": 1,
		},
		{
			"fieldname": "marketplace_shop",
			"label": "Marketplace Shop",
			"fieldtype": "Link",
			"options": "Marketplace Shop",
			"insert_after": "marketplace_order_group",
			"no_copy": 1,
			"read_only": 1,
			"in_list_view": 1,
			"index": 1,
		},
		{
			"fieldname": "marketplace_commission_rate",
			"label": "Commission Rate",
			"fieldtype": "Percent",
			"insert_after": "marketplace_shop",
			"description": "Platform commission snapshot taken at order time.",
			"no_copy": 1,
			"read_only": 1,
		},
		{
			"fieldname": "marketplace_commission_amount",
			"label": "Commission Amount",
			"fieldtype": "Currency",
			"insert_after": "marketplace_commission_rate",
			"no_copy": 1,
			"read_only": 1,
		},
		{
			"fieldname": "vendor_order_status",
			"label": "Vendor Order Status",
			"fieldtype": "Select",
			"options": "New\nProcessing\nReady\nDelivered\nCancelled",
			"default": "New",
			"insert_after": "marketplace_commission_amount",
			"in_list_view": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "vendor_status_updated_on",
			"label": "Vendor Status Updated On",
			"fieldtype": "Datetime",
			"insert_after": "vendor_order_status",
			"no_copy": 1,
			"read_only": 1,
		},
		{
			"fieldname": "marketplace_payout",
			"label": "Marketplace Payout",
			"fieldtype": "Link",
			"options": "Marketplace Payout",
			"insert_after": "vendor_status_updated_on",
			"description": "Settlement this order was paid out in. Set on payout submission.",
			"no_copy": 1,
			"read_only": 1,
			"index": 1,
		},
	],
}


def after_migrate():
	ensure_vendor_role()
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	ensure_platform_shop()


def ensure_vendor_role():
	"""The role vendor accounts carry, for desk-side permissions.

	The app's own vendor gating never trusts roles — it resolves the signed-in
	user's shop — but the role exists so desk users (an admin inspecting a
	shop) and any future desk access follow normal Frappe permission rules.
	"""
	if not frappe.db.exists("Role", VENDOR_ROLE):
		frappe.get_doc(
			{"doctype": "Role", "role_name": VENDOR_ROLE, "desk_access": 0}
		).insert(ignore_permissions=True)


def ensure_platform_shop():
	"""The shop that owns every product not assigned to a vendor.

	Created here because every cart line must resolve to a shop before checkout
	splits it, and a fresh site has no vendors yet. It carries no supplier and
	no commission: it is the platform itself.
	"""
	if frappe.db.get_value("Marketplace Shop", {"is_platform": 1}, "name"):
		return

	doc = frappe.get_doc(
		{
			"doctype": "Marketplace Shop",
			"shop_name": PLATFORM_SHOP_NAME,
			"status": "Approved",
			"is_platform": 1,
			"description": "Platform's own storefront",
		}
	)
	doc.insert(ignore_permissions=True)
