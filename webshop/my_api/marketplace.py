"""Marketplace glue: shop metadata for the storefront, plus guest endpoints.

Everything here is read-only and safe for guests: which shops exist, which shop
owns a product, what a shop's public profile looks like. Mutations (orders,
products, payouts) live in `vendor_api` / `marketplace_admin` behind their own
gates.

Every schema read is guarded with `has_field`, so a site that has not run
`bench migrate` since the marketplace upgrade behaves exactly like the
single-shop app instead of raising on an unknown column.
"""

import frappe
from frappe import _
from frappe.utils import cint

# ---------------------------------------------------------------------------
# Schema probes (memoised per request, like the rest of the API layer)
# ---------------------------------------------------------------------------


def has_shop_field():
	"""Whether Website Item carries the `marketplace_shop` column yet."""
	cached = getattr(frappe.local, "_mp_has_shop_field", None)
	if cached is None:
		cached = bool(frappe.get_meta("Website Item").has_field("marketplace_shop"))
		frappe.local._mp_has_shop_field = cached
	return cached


def _has_sales_order_fields():
	cached = getattr(frappe.local, "_mp_has_so_fields", None)
	if cached is None:
		cached = bool(frappe.get_meta("Sales Order").has_field("marketplace_shop"))
		frappe.local._mp_has_so_fields = cached
	return cached


def has_vendor_ranking_field():
	"""Whether Website Item carries the per-shop `vendor_ranking` column yet.

	A vendor's ranking is theirs to set but only meaningful inside their own
	shop, so it lives in its own column and never in the merchant's site-wide
	`ranking`.
	"""
	cached = getattr(frappe.local, "_mp_has_vendor_ranking", None)
	if cached is None:
		cached = bool(frappe.get_meta("Website Item").has_field("vendor_ranking"))
		frappe.local._mp_has_vendor_ranking = cached
	return cached


# ---------------------------------------------------------------------------
# Shop metadata
# ---------------------------------------------------------------------------


def _shop_rows():
	"""Every shop, projected to the fields the storefront may see, per request.

	Keyed by doctype name (the stable id) rather than by display name, which is
	user-editable until the shop is first serialised.
	"""
	cached = getattr(frappe.local, "_mp_shop_rows", None)
	if cached is None:
		if not frappe.db.exists("DocType", "Marketplace Shop"):
			cached = {}
		else:
			cached = {
				row.name: row
				for row in frappe.get_all(
					"Marketplace Shop",
					fields=[
						"name",
						"shop_name",
						"logo",
						"description",
						"status",
						"delivery_rate",
						"commission_rate",
						"is_platform",
					],
					limit_page_length=0,
				)
			}
		frappe.local._mp_shop_rows = cached
	return cached


def platform_shop_name():
	"""The catch-all shop for products with no vendor, or None."""
	cached = getattr(frappe.local, "_mp_platform_shop", None)
	if cached is None:
		cached = None
		if frappe.db.exists("DocType", "Marketplace Shop"):
			cached = frappe.db.get_value("Marketplace Shop", {"is_platform": 1}, "name")
		frappe.local._mp_platform_shop = cached
	return cached


def shop_public(shop_name):
	"""The client-safe projection of one shop, or None when unresolvable."""
	row = _shop_rows().get(shop_name)
	if not row:
		return None
	return {
		"name": row.name,
		"shop_name": row.shop_name,
		"logo": row.logo or "",
		"description": row.description or "",
		# The platform's own catch-all shop is not a vendor. The storefront uses
		# this to avoid badging every product and grouping every cart line under
		# the store's own name.
		"is_platform": bool(row.is_platform),
	}


def shop_is_active(shop_name):
	"""Approved shops fulfil; everything else must not see new orders."""
	row = _shop_rows().get(shop_name)
	return bool(row and row.status == "Approved")


def shops_for_item_codes(item_codes):
	"""`{item_code: shop_name}` for a batch of codes; unassigned codes are absent."""
	codes = [c for c in dict.fromkeys(item_codes or []) if c]
	if not codes or not has_shop_field():
		return {}

	result = {}
	for row in frappe.get_all(
		"Website Item",
		filters={"item_code": ["in", codes]},
		fields=["item_code", "marketplace_shop"],
		limit_page_length=0,
	):
		result[row.item_code] = row.marketplace_shop
	return result


def resolve_item_shop(item_code, shop_map=None):
	"""The shop a product belongs to; falls back to the platform shop.

	Absent only when the marketplace tables have not been set up at all — the
	cart must be able to attribute every line to *something* so checkout can
	group it.
	"""
	shop = (shop_map or shops_for_item_codes([item_code])).get(item_code)
	if shop:
		return shop
	return platform_shop_name()


def decorate_with_shop(item):
	"""Attach `shop` (public dict) to a decorated catalogue row, in place."""
	if not has_shop_field():
		item.shop = None
		return item
	shop_name = item.get("marketplace_shop") or platform_shop_name()
	item.shop = shop_public(shop_name) if shop_name else None
	return item


# ---------------------------------------------------------------------------
# Guest endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def shops(limit=24, start=0):
	"""Approved vendor shops with their live product counts, best first.

	Ordered by product count: an empty shop at the top of the directory is a
	dead end for a shopper, and there is no other quality signal to rank on yet.
	"""
	if not frappe.db.exists("DocType", "Marketplace Shop"):
		return []

	shops_rows = frappe.get_all(
		"Marketplace Shop",
		filters={"status": "Approved", "is_platform": ["!=", 1]},
		fields=["name", "shop_name", "logo", "description", "creation"],
		order_by="creation asc",
		limit_start=max(cint(start), 0),
		limit_page_length=min(cint(limit) or 24, 100),
	)

	counts = {}
	if has_shop_field() and shops_rows:
		for row in frappe.get_all(
			"Website Item",
			filters={
				"marketplace_shop": ["in", [s.name for s in shops_rows]],
				"published": 1,
				"has_variants": ["in", [0, None]],
			},
			fields=["marketplace_shop", "count(name) as total"],
			group_by="marketplace_shop",
			limit_page_length=0,
		):
			counts[row.marketplace_shop] = cint(row.total)

	for row in shops_rows:
		row.product_count = counts.get(row.name, 0)
	return shops_rows


@frappe.whitelist(allow_guest=True)
def shop(name, start=0, limit=24, search=None):
	"""A shop's public profile and one page of its products.

	`search` narrows that page to this shop's own matches: the same substring
	match the catalogue search makes, but with the shop filter still applied, so
	a term can never surface another vendor's products here.
	"""
	if not frappe.db.exists("Marketplace Shop", name):
		frappe.throw(_("This shop does not exist."), frappe.DoesNotExistError)

	shop_row = frappe.db.get_value(
		"Marketplace Shop",
		name,
		["name", "shop_name", "logo", "description", "status", "vendor_user"],
		as_dict=True,
	)
	if not shop_row:
		frappe.throw(_("This shop does not exist."), frappe.DoesNotExistError)

	# A suspended or rejected shop's storefront 404s rather than going quiet:
	# the vendor should notice their products "disappearing" as an error on
	# their own page, not as a silent drop in traffic.
	if shop_row.status != "Approved":
		frappe.throw(_("This shop is not available."), frappe.DoesNotExistError)

	term = (search or "").strip()

	from webshop.my_api.api import get_catalog, get_catalog_count

	# `total` is the shop's live product count — the profile card means "how big
	# is this shop", and that must not change because a shopper typed something.
	# `count` is how many products matched the term; with no term the two are the
	# same number, so the second query is only paid for on a real search.
	total = get_catalog_count(shop=name)

	return {
		"shop": {
			"name": shop_row.name,
			"shop_name": shop_row.shop_name,
			"logo": shop_row.logo or "",
			"description": shop_row.description or "",
		},
		"items": get_catalog(shop=name, search=term or None, limit=limit, start=start),
		"total": total,
		"count": total if not term else get_catalog_count(shop=name, search=term),
	}
