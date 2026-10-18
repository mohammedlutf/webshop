"""The vendor half of the marketplace: shop profile, orders, products, payouts.

Every endpoint here answers to the signed-in user's own `Marketplace Shop` and
nothing else. There is no argument anywhere that lets a caller name a shop:
ownership is always resolved from the session, and every order/product read is
filtered by it, so one vendor cannot probe another's data by varying a
parameter. Where an object is not owned, the error is "not found" rather than
"not allowed" — an item id must not be probeable either.

Complex arguments (UOM rows, recommended items) arrive as JSON *strings* for
the same reason as in `admin_api`: a string is one value and cannot be
half-parsed by the HTTP layer in front of Frappe.
"""

import re

import frappe
from frappe import _
from frappe.utils import cint, flt, strip_html

from webshop.my_api import marketplace as mp
from webshop.my_api import admin_api

# Vendor statuses a vendor may set. "New" is the starting state and can only be
# left, never set: re-marking a delivered order as new would make it look
# unfulfilled again in every list that trusts this field.
MAY_SET = ("Processing", "Ready", "Delivered", "Cancelled")

_MP_SHOP_FIELDS = (
	"shop_name",
	"status",
	"logo",
	"description",
	"delivery_rate",
	"commission_rate",
	"bank_name",
	"account_holder",
	"account_number",
	"supplier",
	"is_platform",
)


# ---------------------------------------------------------------------------
# Session -> shop
# ---------------------------------------------------------------------------


def _require_login():
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in first."), frappe.AuthenticationError)


def _my_shop(allow_any_status=False):
	"""The signed-in user's shop, memoised for this request.

	An unapproved shop is only returned when the caller explicitly wants it
	(`vendor_state`, profile edits): fulfilment and catalogue endpoints must
	behave as if a pending or suspended shop does not exist.
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		return None

	cached = getattr(frappe.local, "_vendor_shop_doc", None)
	if cached is None:
		cached = None
		if frappe.db.exists("DocType", "Marketplace Shop"):
			name = frappe.db.get_value("Marketplace Shop", {"vendor_user": user}, "name")
			if name:
				cached = frappe.get_doc("Marketplace Shop", name)
		frappe.local._vendor_shop_doc = cached

	if not cached:
		return None
	if not allow_any_status and cached.status != "Approved":
		frappe.throw(
			_("This shop is not active ({0}).").format(cached.status),
			frappe.PermissionError,
		)
	return cached


def _require_shop():
	"""The caller's approved shop, or a clean refusal."""
	shop = _my_shop()
	if not shop:
		frappe.throw(
			_("You do not have an approved shop on this marketplace."),
			frappe.PermissionError,
		)
	return shop


def _owned_website_item(shop, item_code):
	"""The shop's own Website Item for `item_code`, or a clean refusal."""
	item_code = (item_code or "").strip()
	if not item_code or not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item not found."), frappe.DoesNotExistError)
	if not mp.has_shop_field():
		frappe.throw(
			_("The marketplace is not set up on this site yet."), frappe.PermissionError
		)

	website_item = frappe.db.get_value("Website Item", {"item_code": item_code}, "name")
	owner = (
		frappe.db.get_value("Website Item", website_item, "marketplace_shop")
		if website_item
		else None
	)
	# "Not found", never "not yours": another vendor's item code must not be
	# confirmable by an error message.
	if owner != shop.name:
		frappe.throw(_("This product is not available."), frappe.DoesNotExistError)
	return website_item


# ---------------------------------------------------------------------------
# Per-shop product ordering
# ---------------------------------------------------------------------------
#
# `ranking` on Website Item is the merchant's *site-wide* ordering control, so
# a vendor's edit must never write it: one vendor raising their own number
# would otherwise push their product towards the top of the whole catalogue,
# other shops included. A vendor's ordering is kept in its own
# `vendor_ranking` column and is only read back where the listing is already
# scoped to that shop (their own editor, and shop-scoped catalogue pages).


def _vendor_ranking(website_item):
	"""This shop's own ordering for a product (0 until the column exists)."""
	if not website_item or not mp.has_vendor_ranking_field():
		return 0
	return cint(frappe.db.get_value("Website Item", website_item, "vendor_ranking"))


def _set_vendor_ranking(website_item, ranking):
	"""Store the vendor's own ordering.

	Returns warnings instead of writing when the site predates the marketplace
	migration: silently dropping what the vendor typed is worse than telling
	them it did not stick.
	"""
	if not website_item:
		return []
	if not mp.has_vendor_ranking_field():
		return [
			_(
				"The shop ranking was not saved: this site has not been migrated for the marketplace yet."
			)
		]
	frappe.db.set_value("Website Item", website_item, "vendor_ranking", cint(ranking))
	return []


def _shop_projection(shop):
	row = {field: shop.get(field) for field in _MP_SHOP_FIELDS}
	row["name"] = shop.name
	row["delivery_rate"] = flt(shop.delivery_rate)
	row["commission_rate"] = (
		flt(shop.commission_rate)
		if shop.commission_rate is not None
		else flt(frappe.db.get_single_value("Webshop Settings", "default_commission_rate"))
	)
	return row


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------


def _throttle(key, limit, window=60 * 60):
	attempts = cint(frappe.cache().get_value(key) or 0)
	if attempts >= limit:
		frappe.throw(_("Too many attempts. Please try again later."))
	frappe.cache().set_value(key, attempts + 1, expires_in_sec=window)


@frappe.whitelist(allow_guest=True)
def vendor_state():
	"""What the signed-in account is on the marketplace, in one round trip.

	The app calls this once per session to decide between "shopper", "applicant
	in review", "vendor dashboard" and "suspended notice" — each of which is a
	different UI, so a shopper must never have to guess which one they are in.
	"""
	user = frappe.session.user
	if user == "Guest" or not frappe.db.exists("DocType", "Marketplace Shop"):
		return {"is_vendor": False, "status": None, "shop": None, "application": None}

	shop = _my_shop(allow_any_status=True)
	application = frappe.get_all(
		"Marketplace Application",
		filters={"applicant": user},
		fields=["name", "status", "shop_name", "phone", "description", "creation", "review_note"],
		order_by="creation desc",
		limit_page_length=1,
	)
	return {
		"is_vendor": bool(shop),
		"status": shop.status if shop else (application[0].status if application else None),
		"shop": _shop_projection(shop) if shop else None,
		"application": application[0] if application else None,
	}


@frappe.whitelist(methods=["POST"])
def apply_for_shop(shop_name, phone, description=None):
	"""Apply to open a vendor shop. Reviewed by an admin at the desk."""
	_require_login()
	if not frappe.db.exists("DocType", "Marketplace Application"):
		frappe.throw(_("The marketplace is not set up on this site yet."), frappe.PermissionError)

	if _my_shop(allow_any_status=True):
		frappe.throw(_("You already have a shop on this marketplace."))

	pending = frappe.db.get_value(
		"Marketplace Application",
		{"applicant": frappe.session.user, "status": "Pending"},
		"name",
	)
	if pending:
		frappe.throw(_("You already have an application under review."))

	shop_name = strip_html((shop_name or "").strip())[:140]
	phone = re.sub(r"[^\d+]", "", str(phone or "")).strip()
	if not shop_name:
		frappe.throw(_("Shop name is required."))
	if len(re.sub(r"\D", "", phone)) < 6:
		frappe.throw(_("A valid phone number is required."))

	_throttle("ecommerce:vendor_apply:{0}".format(frappe.session.user), limit=5)

	user_full_name = frappe.db.get_value("User", frappe.session.user, "full_name")
	doc = frappe.get_doc(
		{
			"doctype": "Marketplace Application",
			"applicant": frappe.session.user,
			"full_name": user_full_name,
			"phone": phone,
			"shop_name": shop_name,
			"description": (description or "")[:1000],
		}
	)
	doc.insert(ignore_permissions=True)
	return {"name": doc.name, "status": doc.status}


@frappe.whitelist(methods=["POST"])
def cancel_application():
	"""Withdraw a pending application."""
	_require_login()
	name = frappe.db.get_value(
		"Marketplace Application",
		{"applicant": frappe.session.user, "status": "Pending"},
		"name",
	)
	if not name:
		frappe.throw(_("You have no pending application."))
	doc = frappe.get_doc("Marketplace Application", name)
	doc.status = "Cancelled"
	doc.save(ignore_permissions=True)
	return {"name": name, "status": doc.status}


# ---------------------------------------------------------------------------
# Shop profile
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def shop_update(
	description=None,
	delivery_rate=None,
	bank_name=None,
	account_holder=None,
	account_number=None,
	shop_name=None,
	logo_base64=None,
	logo_name=None,
):
	"""Edit the shop's own presentation and payout details.

	Deliberately NOT editable here: the commission rate (that is the
	platform's number), the status, the supplier and the owning user. A vendor
	endpoint that could move any of those would be a privilege escalation with
	a form around it.
	"""
	shop = _my_shop(allow_any_status=True)
	if not shop:
		frappe.throw(_("You do not have a shop on this marketplace."), frappe.PermissionError)

	if shop_name is not None:
		clean = strip_html(shop_name).strip()[:140]
		if not clean:
			frappe.throw(_("Shop name cannot be empty."))
		if clean != shop.shop_name and frappe.db.exists("Marketplace Shop", {"shop_name": clean}):
			frappe.throw(_("Another shop already uses that name."))
		shop.shop_name = clean

	if description is not None:
		shop.description = (description or "")[:1000]
	if delivery_rate is not None:
		rate = flt(delivery_rate)
		if rate < 0:
			frappe.throw(_("The delivery rate cannot be negative."))
		shop.delivery_rate = rate
	for field in ("bank_name", "account_holder", "account_number"):
		value = locals().get(field)
		if value is not None:
			setattr(shop, field, strip_html(value)[:140])

	if logo_base64:
		shop.logo = _save_shop_logo(shop, logo_base64, logo_name)

	shop.flags.ignore_permissions = True
	shop.save()
	return {"ok": True, "shop": _shop_projection(shop)}


def _save_shop_logo(shop, file_base64, file_name=None):
	"""Store the shop logo through the same guarded pipeline as item photos."""
	from frappe.utils.file_manager import save_file

	content, extension = admin_api._decode_image(file_base64)
	stem = admin_api.slugify((file_name or "").rsplit(".", 1)[0]) or "shop-logo"
	filename = "{0}-{1}.{2}".format(stem, frappe.generate_hash(length=8), extension)
	file_doc = save_file(
		fname=filename,
		content=content,
		dt="Marketplace Shop",
		dn=shop.name,
		folder="Home/Attachments",
		is_private=0,
	)
	return file_doc.file_url


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def _so_fields():
	return [
		"name",
		"transaction_date",
		"creation",
		"status",
		"docstatus",
		"grand_total",
		"net_total",
		"currency",
		"customer",
		"contact_mobile",
		"shipping_address_name",
		"marketplace_order_group",
		"vendor_order_status",
		"vendor_status_updated_on",
		"marketplace_commission_rate",
		"marketplace_commission_amount",
	]


def _page_delivery(names):
	"""{order: delivery charge} from the Actual tax rows of one page."""
	if not names:
		return {}
	account = frappe.db.get_single_value("Webshop Settings", "shipping_account")
	result = {}
	for tax in frappe.get_all(
		"Sales Taxes and Charges",
		filters={"parent": ["in", names], "charge_type": "Actual"},
		fields=["parent", "tax_amount", "account_head", "description"],
		limit_page_length=0,
	):
		is_delivery = (account and tax.account_head == account) or (
			"delivery" in (tax.description or "").lower()
		)
		if is_delivery:
			result[tax.parent] = flt(result.get(tax.parent)) + flt(tax.tax_amount)
	return result


def _page_items(names):
	by_order = {}
	if not names:
		return by_order
	for item in frappe.get_all(
		"Sales Order Item",
		filters={"parent": ["in", names], "docstatus": ["<", 2]},
		fields=["parent", "item_code", "item_name", "qty", "uom", "rate", "amount"],
		order_by="idx asc",
	):
		by_order.setdefault(item.parent, []).append(
			{
				"item_code": item.item_code,
				"item_name": item.item_name,
				"qty": item.qty,
				"uom": item.uom,
				"rate": item.rate,
				"amount": item.amount,
			}
		)
	return by_order


def _page_addresses(rows):
	addresses = {}
	names = [row.shipping_address_name for row in rows if row.get("shipping_address_name")]
	if not names:
		return addresses
	for row in frappe.get_all(
		"Address",
		filters={"name": ["in", names]},
		fields=["name", "address_line1", "address_line2", "city", "phone", "pincode"],
		limit_page_length=0,
	):
		addresses[row.name] = row
	return addresses


def _order_row(row, items_by_order, delivery_by_order, addresses):
	address = addresses.get(row.shipping_address_name) or {}
	return {
		"name": row.name,
		"group": row.get("marketplace_order_group") or None,
		"transaction_date": row.transaction_date,
		"status": row.get("vendor_order_status") or "New",
		"erp_status": row.status,
		"grand_total": flt(row.grand_total),
		"net_total": flt(row.net_total),
		"delivery_charges": flt(delivery_by_order.get(row.name)),
		"commission_amount": flt(row.get("marketplace_commission_amount")),
		"currency": row.currency,
		"items": items_by_order.get(row.name, []),
		# The buyer's delivery details travel with the order: in a COD
		# marketplace the vendor is the one who has to reach the customer.
		"customer": {
			"name": row.customer,
			"phone": row.contact_mobile or address.get("phone") or "",
			"city": address.get("city") or "",
			"address": address.get("address_line1") or "",
			"note": address.get("address_line2") or "",
		},
	}


@frappe.whitelist()
def vendor_orders(start=0, page_length=12, vendor_status=None):
	"""One page of this shop's orders, newest first."""
	shop = _require_shop()
	start = max(cint(start) or 0, 0)
	page_length = min(max(cint(page_length) or 12, 1), 100)

	filters = {"marketplace_shop": shop.name, "docstatus": 1}
	if vendor_status:
		if vendor_status not in MAY_SET + ("New",):
			frappe.throw(_("Unknown order status."))
		filters["vendor_order_status"] = vendor_status

	total = frappe.db.count("Sales Order", filters)
	rows = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=_so_fields(),
		order_by="transaction_date desc, creation desc",
		limit_start=start,
		limit_page_length=page_length,
	)
	names = [row.name for row in rows]
	items = _page_items(names)
	delivery = _page_delivery(names)
	addresses = _page_addresses(rows)

	return {
		"orders": [_order_row(row, items, delivery, addresses) for row in rows],
		"total": total,
		"start": start,
		"page_length": page_length,
		"has_more": start + len(rows) < total,
	}


@frappe.whitelist(methods=["POST"])
def vendor_update_order_status(order_name, vendor_status):
	"""Advance one of this shop's orders through fulfilment.

	Cancelling is executed as a real Sales Order cancellation (stock and
	docstatus stay honest); every other transition is a status stamp the
	customer-facing group summary reads.
	"""
	shop = _require_shop()
	if vendor_status not in MAY_SET:
		frappe.throw(_("Unknown order status."))

	order = frappe.db.get_value(
		"Sales Order",
		order_name,
		["name", "marketplace_shop", "docstatus", "vendor_order_status", "status"],
		as_dict=True,
	)
	if not order or order.marketplace_shop != shop.name:
		frappe.throw(_("This order is not available."), frappe.DoesNotExistError)
	if order.docstatus != 1:
		frappe.throw(_("This order can no longer be updated."))
	if order.vendor_order_status == "Delivered" and vendor_status != "Delivered":
		frappe.throw(_("A delivered order cannot go back to `{0}`.").format(vendor_status))
	if order.vendor_order_status == "Cancelled":
		frappe.throw(_("This order was cancelled."))

	if vendor_status == "Cancelled":
		doc = frappe.get_doc("Sales Order", order_name)
		doc.flags.ignore_permissions = True
		doc.cancel()

	frappe.db.set_value(
		"Sales Order",
		order_name,
		{
			"vendor_order_status": vendor_status,
			"vendor_status_updated_on": frappe.utils.now(),
		},
		update_modified=False,
	)
	return {"name": order_name, "status": vendor_status}


@frappe.whitelist()
def vendor_order(order_name):
	"""One order in the same shape as the list rows."""
	shop = _require_shop()
	row = frappe.db.get_value(
		"Sales Order",
		{"name": order_name, "marketplace_shop": shop.name},
		_so_fields(),
		as_dict=True,
	)
	if not row:
		frappe.throw(_("This order is not available."), frappe.DoesNotExistError)
	return _order_row(
		row,
		_page_items([row.name]),
		_page_delivery([row.name]),
		_page_addresses([row]),
	)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@frappe.whitelist()
def vendor_dashboard(days=30):
	"""Sales, fulfilment state and top products for the recent period.

	Aggregated in the database: a shop with a thousand orders must not ship a
	thousand rows to the app so the app can add them up.
	"""
	shop = _require_shop()
	days = min(max(cint(days) or 30, 1), 365)
	from_date = frappe.utils.add_days(frappe.utils.nowdate(), -days)

	totals = frappe.db.sql(
		"""
		SELECT COUNT(name) AS order_count,
		       IFNULL(SUM(grand_total), 0) AS sales,
		       IFNULL(SUM(marketplace_commission_amount), 0) AS commission,
		       IFNULL(SUM(CASE WHEN vendor_order_status = 'Delivered' THEN 1 ELSE 0 END), 0) AS delivered,
		       IFNULL(SUM(CASE WHEN vendor_order_status = 'Cancelled' THEN 1 ELSE 0 END), 0) AS cancelled,
		       IFNULL(SUM(CASE WHEN IFNULL(vendor_order_status, 'New') IN ('New', 'Processing', 'Ready')
		                THEN 1 ELSE 0 END), 0) AS open_orders,
		       IFNULL(SUM(CASE WHEN IFNULL(vendor_order_status, 'New') IN ('New', 'Processing')
		                THEN 1 ELSE 0 END), 0) AS to_process
		FROM `tabSales Order`
		WHERE marketplace_shop = %s
		  AND docstatus = 1
		  AND transaction_date >= %s
		""",
		(shop.name, from_date),
		as_dict=True,
	)[0]

	top_products = frappe.db.sql(
		"""
		SELECT soi.item_name AS item_name,
		       SUM(soi.qty) AS qty,
		       IFNULL(SUM(soi.amount), 0) AS amount
		FROM `tabSales Order Item` soi
		INNER JOIN `tabSales Order` so ON so.name = soi.parent
		WHERE so.marketplace_shop = %s
		  AND so.docstatus = 1
		  AND so.transaction_date >= %s
		  AND IFNULL(so.vendor_order_status, 'New') != 'Cancelled'
		GROUP BY soi.item_name
		ORDER BY amount DESC
		LIMIT 5
		""",
		(shop.name, from_date),
		as_dict=True,
	)

	recent = frappe.get_all(
		"Sales Order",
		filters={
			"marketplace_shop": shop.name,
			"docstatus": 1,
			"vendor_order_status": ["!=", "Cancelled"],
		},
		fields=["name", "transaction_date", "grand_total", "currency", "vendor_order_status"],
		order_by="transaction_date desc, creation desc",
		limit_page_length=5,
	)

	product_count = (
		frappe.db.count("Website Item", {"marketplace_shop": shop.name, "published": 1})
		if mp.has_shop_field()
		else 0
	)

	return {
		"period_days": days,
		"from_date": from_date,
		"sales": flt(totals.sales),
		"commission": flt(totals.commission),
		"net": flt(totals.sales) - flt(totals.commission),
		"order_count": cint(totals.order_count),
		"open_orders": cint(totals.open_orders),
		"to_process": cint(totals.to_process),
		"delivered": cint(totals.delivered),
		"cancelled": cint(totals.cancelled),
		"product_count": product_count,
		"top_products": [
			{"item_name": row.item_name, "qty": flt(row.qty), "amount": flt(row.amount)}
			for row in top_products
		],
		"recent_orders": [
			{
				"name": row.name,
				"transaction_date": row.transaction_date,
				"grand_total": flt(row.grand_total),
				"currency": row.currency,
				"status": row.vendor_order_status or "New",
			}
			for row in recent
		],
	}


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


@frappe.whitelist()
def vendor_reference_data():
	"""Item groups, UOMs and warehouses for the vendor's create-product form."""
	_require_shop()
	return admin_api._reference_data()


@frappe.whitelist()
def vendor_products(start=0, page_length=20, search=None):
	"""One page of this shop's products, newest first."""
	shop = _require_shop()
	start = max(cint(start) or 0, 0)
	page_length = min(max(cint(page_length) or 20, 1), 100)

	fields = [
		"name",
		"item_code",
		"web_item_name",
		"item_name",
		"item_group",
		"website_image",
		"thumbnail",
		"short_description",
		"published",
		"modified",
	]
	# The vendor's own ordering is reported under `ranking`, so this API only
	# ever surfaces what the shop controls — never the merchant's site-wide
	# column, which vendors cannot set.
	has_vendor_ranking = mp.has_vendor_ranking_field()
	if has_vendor_ranking:
		fields.append("vendor_ranking")
	if frappe.get_meta("Website Item").has_field("blurhash"):
		fields.append("blurhash")

	filters = {"marketplace_shop": shop.name}
	or_filters = None
	if search:
		like = f"%{search}%"
		or_filters = [
			{"web_item_name": ["like", like]},
			{"item_name": ["like", like]},
			{"item_code": ["like", like]},
		]

	total_rows = frappe.get_all(
		"Website Item",
		filters=filters,
		or_filters=or_filters,
		fields=["count(name) as total"],
		limit_page_length=0,
	)
	rows = frappe.get_all(
		"Website Item",
		filters=filters,
		or_filters=or_filters,
		fields=fields,
		order_by="modified desc",
		limit_start=start,
		limit_page_length=page_length,
	)
	for row in rows:
		row.image = row.website_image or row.thumbnail or ""
		row["ranking"] = cint(row.pop("vendor_ranking") or 0) if has_vendor_ranking else 0

	return {
		"items": rows,
		"total": cint(total_rows[0].total) if total_rows else 0,
		"start": start,
		"page_length": page_length,
		"has_more": start + len(rows) < (cint(total_rows[0].total) if total_rows else 0),
	}


@frappe.whitelist()
def vendor_product_payload(item_code=None):
	"""The full editor payload for one of this shop's products."""
	shop = _require_shop()
	website_item = _owned_website_item(shop, item_code)
	payload = admin_api._item_payload(item_code)
	# The editor's ranking field is this shop's own ordering, so it is reported
	# from `vendor_ranking` and not from the merchant's site-wide `ranking`.
	if payload.get("website_item"):
		payload["website_item"]["ranking"] = _vendor_ranking(website_item)
	return payload


@frappe.whitelist(methods=["POST"])
def vendor_create_product(
	item_code=None,
	item_name=None,
	item_group=None,
	stock_uom=None,
	short_description=None,
	web_long_description=None,
	ranking=None,
	price_list_rate=None,
	website_warehouse=None,
	image_base64=None,
	image_name=None,
	uoms_json=None,
	recommended_json=None,
	published=1,
):
	"""Create a product that belongs to the caller's shop.

	The whole item/price/website-item pipeline is the admin one — the rules for
	what a valid product is must not fork between who created it. Two
	differences: the shop is stamped from the session and can never be supplied
	by the client, and the ranking goes to this shop's `vendor_ranking` rather
	than the merchant's site-wide `ranking`.
	"""
	shop = _require_shop()
	result = admin_api._create_item(
		item_code=item_code,
		item_name=item_name,
		item_group=item_group,
		stock_uom=stock_uom,
		short_description=short_description,
		web_long_description=web_long_description,
		price_list_rate=price_list_rate,
		website_warehouse=website_warehouse,
		image_base64=image_base64,
		image_name=image_name,
		uoms_json=uoms_json,
		recommended_json=recommended_json,
		published=published,
	)
	if result.get("website_item"):
		frappe.db.set_value(
			"Website Item", result["website_item"], "marketplace_shop", shop.name
		)
		warnings = _set_vendor_ranking(result["website_item"], ranking or 0)
		if warnings:
			result["warnings"] = list(result.get("warnings") or []) + warnings
	return result


@frappe.whitelist(methods=["POST"])
def vendor_update_product(
	item_code=None,
	item_name=None,
	item_group=None,
	short_description=None,
	web_long_description=None,
	ranking=None,
	published=None,
	image_base64=None,
	image_name=None,
	uoms_json=None,
	recommended_json=None,
):
	"""Edit one of this shop's products. Same pipeline, ownership-checked."""
	shop = _require_shop()
	website_item = _owned_website_item(shop, item_code)
	# `ranking` is not forwarded: that would write the merchant's site-wide
	# ordering. It is applied to this shop's own column below instead.
	result = admin_api._update_item(
		item_code=item_code,
		item_name=item_name,
		item_group=item_group,
		short_description=short_description,
		web_long_description=web_long_description,
		published=published,
		image_base64=image_base64,
		image_name=image_name,
		uoms_json=uoms_json,
		recommended_json=recommended_json,
	)
	if ranking is not None:
		warnings = _set_vendor_ranking(website_item, ranking)
		if warnings:
			result["warnings"] = list(result.get("warnings") or []) + warnings
	return result


# ---------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------


def _payout_fields():
	return [
		"name",
		"from_date",
		"to_date",
		"docstatus",
		"status_display",
		"total_gross",
		"total_commission",
		"total_net",
		"payment_reference",
		"paid_on",
		"creation",
	]


def _payout_row(row):
	if row.docstatus == 0:
		row.status = "Draft"
	elif row.docstatus == 2:
		row.status = "Cancelled"
	else:
		row.status = row.status_display or ("Paid" if row.paid_on else "Unpaid")
	return row


@frappe.whitelist()
def vendor_payouts(start=0, page_length=12):
	"""This shop's settlements, newest first."""
	shop = _require_shop()
	if not frappe.db.exists("DocType", "Marketplace Payout"):
		return {"payouts": [], "total": 0, "start": 0, "page_length": page_length, "has_more": False}

	start = max(cint(start) or 0, 0)
	page_length = min(max(cint(page_length) or 12, 1), 100)

	filters = {"shop": shop.name}
	total = frappe.db.count("Marketplace Payout", filters)
	rows = frappe.get_all(
		"Marketplace Payout",
		filters=filters,
		fields=_payout_fields(),
		order_by="creation desc",
		limit_start=start,
		limit_page_length=page_length,
	)
	for row in rows:
		_payout_row(row)
	return {
		"payouts": rows,
		"total": total,
		"start": start,
		"page_length": page_length,
		"has_more": start + len(rows) < total,
	}


@frappe.whitelist()
def vendor_payout_payload(name):
	"""One payout with its order rows."""
	shop = _require_shop()
	row = frappe.db.get_value(
		"Marketplace Payout", {"name": name, "shop": shop.name}, _payout_fields(), as_dict=True
	)
	if not row:
		frappe.throw(_("This payout is not available."), frappe.DoesNotExistError)
	row = frappe._dict(row)
	_payout_row(row)
	row.orders = frappe.get_all(
		"Marketplace Payout Order",
		filters={"parent": name, "parenttype": "Marketplace Payout"},
		fields=[
			"sales_order",
			"order_date",
			"customer",
			"gross_amount",
			"commission_rate",
			"commission_amount",
			"net_amount",
		],
		order_by="order_date desc",
	)
	row.shop = _shop_projection(shop)
	return row
