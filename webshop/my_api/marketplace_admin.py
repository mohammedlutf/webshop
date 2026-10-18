"""Marketplace administration: shops, vendor applications and payouts.

Every endpoint here is behind `admin_api._require_admin()`, the same gate the
catalogue admin uses, so "who may run the marketplace" is decided in one place.

The reads are schema-tolerant: if the marketplace tables have not been created
yet (site has not run `bench migrate` since the upgrade) the list endpoints
return empty results and the mutating ones refuse with a 417, instead of
raising a 500 that looks like a bug in this module. Nothing here trusts a
client-supplied shop name either: transitions are looked up by primary key and
re-read after the change.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate

from webshop.my_api import admin_api

# Marketplace Payout docstatus, spelled out: the UI filter passes a word.
PAYOUT_STATUS_FILTERS = {
	"Draft": {"docstatus": 0},
	"Submitted": {"docstatus": 1, "paid_on": ["is", "not set"]},
	"Paid": {"docstatus": 1, "paid_on": ["set"]},
	"Cancelled": {"docstatus": 2},
}


# ---------------------------------------------------------------------------
# Schema probes
# ---------------------------------------------------------------------------


def _mp_doctype_exists(name):
	"""Whether the marketplace doctype has actually been migrated in yet.

	Cached per request like the other schema probes: several endpoints ask
	more than once (list -> payload), and a `DocType` lookup is a query each.
	"""
	cache_key = "_mp_exists_" + name.replace(" ", "_").lower()
	cached = getattr(frappe.local, cache_key, None)
	if cached is None:
		cached = bool(frappe.db.exists("DocType", name))
		setattr(frappe.local, cache_key, cached)
	return cached


def _require_mp_doctype(name):
	"""The doctype, or a clean "not set up yet" refusal."""
	if not _mp_doctype_exists(name):
		frappe.throw(
			_("The marketplace is not set up on this site yet."),
			frappe.PermissionError,
		)


def _shop_meta_fields():
	"""Marketplace Shop fields that exist on this site, cached per request."""
	cached = getattr(frappe.local, "_mp_shop_meta_fields", None)
	if cached is None:
		cached = (
			[df.fieldname for df in frappe.get_meta("Marketplace Shop").fields]
			if _mp_doctype_exists("Marketplace Shop")
			else []
		)
		frappe.local._mp_shop_meta_fields = cached
	return cached


def _so_has_shop_field():
	"""Whether Sales Order carries the marketplace columns yet."""
	cached = getattr(frappe.local, "_mp_so_has_shop", None)
	if cached is None:
		cached = bool(frappe.get_meta("Sales Order").has_field("marketplace_shop"))
		frappe.local._mp_so_has_shop = cached
	return cached


def _wi_has_shop_field():
	cached = getattr(frappe.local, "_mp_wi_has_shop", None)
	if cached is None:
		cached = bool(frappe.get_meta("Website Item").has_field("marketplace_shop"))
		frappe.local._mp_wi_has_shop = cached
	return cached


def _require_admin():
	"""The catalogue-admin gate, reused: one definition of "may administer"."""
	admin_api._require_admin()


# ---------------------------------------------------------------------------
# Shop payloads
# ---------------------------------------------------------------------------


def _shop_payload(name):
	"""One shop, projected for the admin panel."""
	if not name or not _mp_doctype_exists("Marketplace Shop"):
		return {}

	row = frappe.db.get_value(
		"Marketplace Shop",
		name,
		["*"],
		as_dict=True,
	)
	if not row:
		return {}

	data = {k: v for k, v in row.items() if not k.startswith("_")}
	data["is_platform"] = cint(data.get("is_platform"))
	data["logo"] = data.get("logo") or ""
	data["description"] = data.get("description") or ""
	data["status"] = data.get("status") or "Pending"

	if data.get("vendor_user"):
		data["vendor_user_fullname"] = frappe.db.get_value(
			"User", data["vendor_user"], "full_name"
		)
	else:
		data["vendor_user_fullname"] = None

	# Counts the panel shows on the shop card. Both reads are conditional on
	# the columns existing so a half-migrated site degrades to None.
	if _wi_has_shop_field():
		data["product_count"] = frappe.db.count(
			"Website Item", {"marketplace_shop": name, "published": 1}
		)
		data["unpublished_product_count"] = frappe.db.count(
			"Website Item", {"marketplace_shop": name, "published": 0}
		)
	else:
		data["product_count"] = None
		data["unpublished_product_count"] = None

	if _so_has_shop_field():
		data["order_count"] = frappe.db.count(
			"Sales Order", {"marketplace_shop": name, "docstatus": 1}
		)
	else:
		data["order_count"] = None

	if data.get("status") == "Approved" and not data.get("supplier") and not data.get("is_platform"):
		# A shop approved before `ensure_supplier` existed: show what a
		# re-approval would create, so the admin is not left guessing why
		# settlement documents cannot be made for it.
		data["supplier_missing"] = True
	else:
		data["supplier_missing"] = False

	data["open_application"] = _open_application_for(name)
	return data


def _open_application_for(shop_name):
	"""The pending application behind a shop, if one exists."""
	if not _mp_doctype_exists("Marketplace Application"):
		return None
	app = frappe.db.get_value(
		"Marketplace Application",
		{"shop_name": shop_name, "status": "Pending"},
		["name", "applicant", "phone", "description", "creation"],
		as_dict=True,
	)
	return app


# ---------------------------------------------------------------------------
# Shops
# ---------------------------------------------------------------------------


@frappe.whitelist()
def admin_list_shops(status=None, search=None, limit=20, start=0):
	"""Every shop, filtered, for the admin panel."""
	_require_admin()
	if not _mp_doctype_exists("Marketplace Shop"):
		return {"shops": [], "total": 0, "start": 0, "limit": limit, "has_more": False}

	limit = min(max(cint(limit) or 20, 1), 100)
	start = max(cint(start) or 0, 0)

	filters = {}
	if status and status != "All":
		filters["status"] = status

	or_filters = None
	if search:
		like = f"%{search}%"
		or_filters = [
			{"shop_name": ["like", like]},
			{"vendor_user": ["like", like]},
			{"supplier": ["like", like]},
		]

	total = len(
        frappe.get_all(
            "Marketplace Shop",
            filters=filters,
            or_filters=or_filters,
            fields=["name"],
        )
    )
	rows = frappe.get_all(
		"Marketplace Shop",
		filters=filters,
		or_filters=or_filters,
		fields=["name"],
		order_by="status, shop_name",
		limit_start=start,
		limit_page_length=limit,
	)
	shops = [_shop_payload(row.name) for row in rows]

	return {
		"shops": shops,
		"total": total,
		"start": start,
		"limit": limit,
		"has_more": start + len(rows) < total,
		"platform_shop": _platform_shop_name(),
	}


def _platform_shop_name():
	"""The catch-all shop for products with no vendor, or None."""
	if not _mp_doctype_exists("Marketplace Shop"):
		return None
	return frappe.db.get_value("Marketplace Shop", {"is_platform": 1}, "name")


@frappe.whitelist()
def admin_shop(name):
	"""One shop in full, for the detail panel."""
	_require_admin()
	_require_mp_doctype("Marketplace Shop")
	data = _shop_payload(name)
	if not data:
		frappe.throw(_("This shop does not exist."), frappe.DoesNotExistError)
	return data


@frappe.whitelist()
def admin_shop_update(name, payload):
	"""Edit a shop's admin-managed fields (rates, notes, payout details)."""
	_require_admin()
	_require_mp_doctype("Marketplace Shop")
	if isinstance(payload, str):
		import json

		try:
			payload = json.loads(payload)
		except ValueError:
			frappe.throw(_("Invalid payload."), frappe.ValidationError)

	# Only fields the doctype actually declares, so a crafted payload cannot
	# write `is_platform` or `supplier` through this endpoint.
	editable = {
		"shop_name",
		"description",
		"logo",
		"vendor_user",
		"commission_rate",
		"delivery_rate",
		"bank_name",
		"account_holder",
		"account_number",
		"rejection_reason",
	}
	allowed = editable & set(_shop_meta_fields())

	values = {k: v for k, v in (payload or {}).items() if k in allowed}
	if not values:
		frappe.throw(_("Nothing to update."), frappe.ValidationError)

	frappe.db.set_value("Marketplace Shop", name, values)
	return _shop_payload(name)


def _transition(name, action):
	"""Load a shop, run a state transition, return the fresh payload."""
	_require_mp_doctype("Marketplace Shop")
	if not frappe.db.exists("Marketplace Shop", name):
		frappe.throw(_("This shop does not exist."), frappe.DoesNotExistError)

	shop = frappe.get_doc("Marketplace Shop", name)
	if action == "approve":
		shop.approve()
	elif action == "suspend":
		shop.suspend()
	elif action == "reinstate":
		shop.reinstate()
	else:
		frappe.throw(_("Unknown action."), frappe.ValidationError)

	frappe.db.commit()
	return _shop_payload(name)


@frappe.whitelist()
def admin_approve_shop(name):
	"""Approve a shop, creating its Supplier in the same transaction.

	A shop approved this way inherits nothing from an application: the admin is
	acting on the shop record itself (a shop created by hand at the desk, or a
	shop whose application was already resolved). The pending application, if
	any, is closed so the review queue does not keep showing work that is done.
	"""
	_require_admin()
	data = _transition(name, "approve")
	_close_open_applications(data["shop_name"])
	return data


@frappe.whitelist()
def admin_suspend_shop(name):
	_require_admin()
	return _transition(name, "suspend")


@frappe.whitelist()
def admin_reinstate_shop(name):
	_require_admin()
	return _transition(name, "reinstate")


def _close_open_applications(shop_name):
	"""Mark a shop's pending applications as reviewed.

	Called after a transition on the *shop*, which may have been approved
	without going through the application queue. Leaving the queue stale would
	invite a second approval that trips the one-shop-per-account rule.
	"""
	if not _mp_doctype_exists("Marketplace Application"):
		return
	names = frappe.get_all(
		"Marketplace Application",
		filters={"shop_name": shop_name, "status": "Pending"},
		pluck="name",
	)
	for app_name in names:
		app = frappe.get_doc("Marketplace Application", app_name)
		app.status = "Approved"
		app.shop = app.shop or shop_name
		app.reviewed_by = frappe.session.user
		app.reviewed_on = nowdate()
		app.flags.ignore_permissions = True
		app.save()


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------


def _application_payload(name):
	"""One application, projected for the review queue."""
	if not _mp_doctype_exists("Marketplace Application"):
		return {}
	row = frappe.db.get_value(
		"Marketplace Application",
		name,
		["*"],
		as_dict=True,
	)
	if not row:
		return {}
	data = {k: v for k, v in row.items() if not k.startswith("_")}
	if data.get("applicant"):
		data["applicant_fullname"] = frappe.db.get_value(
			"User", data["applicant"], "full_name"
		)
	else:
		data["applicant_fullname"] = None
	data["is_pending"] = (data.get("status") or "") == "Pending"
	return data


@frappe.whitelist()
def admin_list_applications(status="Pending", limit=20, start=0):
	"""The review queue, pending first."""
	_require_admin()
	if not _mp_doctype_exists("Marketplace Application"):
		return {"applications": [], "total": 0, "start": 0, "limit": limit, "has_more": False}

	limit = min(max(cint(limit) or 20, 1), 100)
	start = max(cint(start) or 0, 0)

	filters = {}
	if status and status != "All":
		filters["status"] = status

	total = frappe.db.count("Marketplace Application", filters=filters)
	rows = frappe.get_all(
		"Marketplace Application",
		filters=filters,
		fields=["name"],
		order_by="creation desc",
		limit_start=start,
		limit_page_length=limit,
	)
	applications = [_application_payload(row.name) for row in rows]

	return {
		"applications": applications,
		"total": total,
		"start": start,
		"limit": limit,
		"has_more": start + len(rows) < total,
		"pending_count": frappe.db.count(
			"Marketplace Application", {"status": "Pending"}
		)
		if _mp_doctype_exists("Marketplace Application")
		else 0,
	}


@frappe.whitelist()
def admin_application(name):
	_require_admin()
	_require_mp_doctype("Marketplace Application")
	data = _application_payload(name)
	if not data:
		frappe.throw(_("This application does not exist."), frappe.DoesNotExistError)
	return data


@frappe.whitelist()
def admin_approve_application(name):
	"""Approve an application: creates the shop (and its Supplier) with it."""
	_require_admin()
	_require_mp_doctype("Marketplace Application")
	app = frappe.get_doc("Marketplace Application", name)
	shop_name = app.approve()
	frappe.db.commit()
	return {"application": _application_payload(name), "shop": _shop_payload(shop_name)}


@frappe.whitelist()
def admin_reject_application(name, reason=None):
	_require_admin()
	_require_mp_doctype("Marketplace Application")
	app = frappe.get_doc("Marketplace Application", name)
	app.reject(reason)
	frappe.db.commit()
	return _application_payload(name)


# ---------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------


def _payout_payload(name):
	"""One payout with its order rows, for the settlement panel."""
	if not _mp_doctype_exists("Marketplace Payout"):
		return {}
	row = frappe.db.get_value(
		"Marketplace Payout",
		name,
		["*"],
		as_dict=True,
	)
	if not row:
		return {}
	data = {k: v for k, v in row.items() if not k.startswith("_")}

	# A human status derived from docstate plus the settlement stamp, the same
	# way the vendor side reads it: the desk's "Submitted" alone does not say
	# whether the money has actually gone out.
	if data.get("docstatus") == 0:
		data["status"] = "Draft"
	elif data.get("docstatus") == 2:
		data["status"] = "Cancelled"
	elif data.get("paid_on"):
		data["status"] = "Paid"
	else:
		data["status"] = "Submitted"

	data["shop_name"] = frappe.db.get_value(
		"Marketplace Shop", data.get("shop"), "shop_name"
	)
	data["orders"] = frappe.get_all(
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
	return data


@frappe.whitelist()
def admin_list_payouts(status=None, shop=None, limit=20, start=0):
	"""Payouts across every shop, newest first."""
	_require_admin()
	if not _mp_doctype_exists("Marketplace Payout"):
		return {"payouts": [], "total": 0, "start": 0, "limit": limit, "has_more": False}

	limit = min(max(cint(limit) or 20, 1), 100)
	start = max(cint(start) or 0, 0)

	filters = {}
	if shop:
		filters["shop"] = shop
	if status and status != "All":
		filters.update(PAYOUT_STATUS_FILTERS.get(status, {}))

	total = frappe.db.count("Marketplace Payout", filters=filters)
	rows = frappe.get_all(
		"Marketplace Payout",
		filters=filters,
		fields=["name"],
		order_by="creation desc",
		limit_start=start,
		limit_page_length=limit,
	)
	payouts = [_payout_payload(row.name) for row in rows]

	return {
		"payouts": payouts,
		"total": total,
		"start": start,
		"limit": limit,
		"has_more": start + len(rows) < total,
	}


@frappe.whitelist()
def admin_payout(name):
	_require_admin()
	_require_mp_doctype("Marketplace Payout")
	data = _payout_payload(name)
	if not data:
		frappe.throw(_("This payout does not exist."), frappe.DoesNotExistError)
	return data


def _eligible_orders(shop, from_date, to_date):
	"""Delivered, unpaid orders of `shop` in the window, newest first.

	The commission snapshot written at order time is read back rather than
	recomputed from the shop's current rate: a rate change must not re-price
	orders that were already delivered under the old one.
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


@frappe.whitelist()
def admin_payout_eligible_orders(shop, from_date, to_date):
	"""What a payout for `shop` over the period would include, and its totals.

	Returned without writing anything, so the panel can show the amounts before
	the admin commits to a run.
	"""
	_require_admin()
	if not _so_has_shop_field():
		return {"orders": [], "total_gross": 0, "total_commission": 0, "total_net": 0}

	rows = _eligible_orders(shop, from_date, to_date)
	total_gross = total_commission = 0.0
	orders = []
	for row in rows:
		gross = flt(row.net_total)
		commission = flt(row.marketplace_commission_amount)
		if not commission and row.marketplace_commission_rate is not None:
			commission = gross * flt(row.marketplace_commission_rate) / 100.0
		total_gross += gross
		total_commission += commission
		orders.append(
			{
				"sales_order": row.name,
				"order_date": row.transaction_date,
				"customer": row.customer,
				"gross_amount": gross,
				"commission_rate": row.marketplace_commission_rate,
				"commission_amount": commission,
				"net_amount": gross - commission,
				"currency": row.currency,
			}
		)

	return {
		"orders": orders,
		"total_gross": total_gross,
		"total_commission": total_commission,
		"total_net": total_gross - total_commission,
	}


@frappe.whitelist()
def admin_create_payout(shop, from_date, to_date, sales_orders=None):
	"""Build a draft payout from the delivered, unpaid orders of a period.

	The rows are recomputed server-side on validate; the optional
	`sales_orders` argument only *narrows* the set (the admin may want to pay
	out part of a period), never widens it — anything not in the eligible set
	is refused by the payout's own checks rather than silently dropped.
	"""
	_require_admin()
	_require_mp_doctype("Marketplace Payout")

	if isinstance(sales_orders, str):
		import json

		try:
			sales_orders = json.loads(sales_orders) if sales_orders else None
		except ValueError:
			frappe.throw(_("Invalid order list."), frappe.ValidationError)

	eligible = _eligible_orders(shop, from_date, to_date)
	if sales_orders:
		wanted = set(sales_orders)
		eligible = [row for row in eligible if row.name in wanted]

	if not eligible:
		frappe.throw(
			_("No delivered, unpaid orders for `{0}` in this period.").format(shop),
			frappe.ValidationError,
		)

	doc = frappe.get_doc(
		{
			"doctype": "Marketplace Payout",
			"shop": shop,
			"from_date": from_date,
			"to_date": to_date,
			"orders": [
				{
					"sales_order": row.name,
					"order_date": row.transaction_date,
					"customer": row.customer,
					"gross_amount": flt(row.net_total),
					"commission_rate": row.marketplace_commission_rate,
					"commission_amount": flt(row.marketplace_commission_amount),
					"net_amount": flt(row.net_total) - flt(row.marketplace_commission_amount),
				}
				for row in eligible
			],
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return _payout_payload(doc.name)


@frappe.whitelist()
def admin_submit_payout(name):
	"""Submit a payout, stamping its Sales Orders with the backlink."""
	_require_admin()
	_require_mp_doctype("Marketplace Payout")
	doc = frappe.get_doc("Marketplace Payout", name)
	doc.submit()
	frappe.db.commit()
	return _payout_payload(name)


@frappe.whitelist()
def admin_cancel_payout(name):
	"""Cancel a payout, releasing its orders back into the unpaid pool."""
	_require_admin()
	_require_mp_doctype("Marketplace Payout")
	doc = frappe.get_doc("Marketplace Payout", name)
	doc.flags.ignore_permissions = True
	doc.cancel()
	frappe.db.commit()
	return _payout_payload(name)


@frappe.whitelist()
def admin_mark_payout_paid(name, payment_reference=None):
	"""Record that a submitted payout has been settled."""
	_require_admin()
	_require_mp_doctype("Marketplace Payout")
	doc = frappe.get_doc("Marketplace Payout", name)
	doc.mark_paid(payment_reference)
	frappe.db.commit()
	return _payout_payload(name)
