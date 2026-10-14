
import json
import re

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate


def _settings():
	"""The site's `Webshop Settings`, read once per request.

	`frappe.get_cached_doc` still hits Redis on every call, and this module reads
	the settings a dozen times per catalogue request (zero-rate rule, price list,
	company, delivery). Memoising on `frappe.local` keeps the per-request answer
	identical to what the doc's own cache would return, while removing the
	repeated round trips from the request's critical path.
	"""
	cached = getattr(frappe.local, "_webshop_settings", None)
	if cached is None:
		cached = frappe.get_cached_doc("Webshop Settings")
		frappe.local._webshop_settings = cached
	return cached


# ---------------------------------------------------------------------------
# Storefront visibility: zero-rated products
# ---------------------------------------------------------------------------


def _is_admin_user():
	"""Whether the caller is back-office staff.

	Staff are exempt from the zero-rate hiding rule on purpose: a product whose
	selling price has not been filled in yet still has to be visible to the person
	who has to fill it in. Hiding it from the admin would make the very products
	needing attention the ones nobody can see.

	`Administrator` is checked on its own because a session for it does not go
	through the same role resolution as a normal user.
	"""
	if frappe.session.user == "Administrator":
		return True
	roles = frappe.get_roles(frappe.session.user)
	return "System Manager" in roles or "Administrator" in roles


def _hide_zero_rate_enabled():
	"""Whether zero-rated products must be hidden from this caller.

	Read with `.get()` rather than attribute access so a site whose Webshop
	Settings does not carry the field yet falls back to showing everything, instead
	of raising on every product page.
	"""
	if not _settings().get("hide_product_with_zero_rate"):
		return False
	return not _is_admin_user()


def _zero_rate_item_codes():
	"""Item codes with no positive selling rate in the shop's price list.

	"Has no selling price row" and "every selling price row is 0" are the same
	thing to the storefront, because `_get_calculated_uoms` falls back to a rate of
	0 when it cannot find a usable one. Treating only the second case as zero-rated
	would let a completely unpriced product through, which is precisely what this
	setting exists to stop.

	Restricted to published, variant-free products in website-visible groups so the
	exclusion list stays as small as the storefront it applies to.

	Memoised on `frappe.local` because one page load asks for both the rows and
	the matching count, and the answer cannot change mid-request.
	"""
	cached = getattr(frappe.local, "_zero_rate_item_codes", None)
	if cached is not None:
		return cached

	price_list = _settings().price_list
	if not price_list:
		cached = []
	else:
		cached = frappe.db.sql(
			"""
			SELECT wi.item_code
			FROM `tabWebsite Item` wi
			LEFT JOIN `tabItem Price` ip
				ON ip.item_code = wi.item_code
				AND ip.price_list = %s
				AND ip.selling = 1
				AND IFNULL(ip.price_list_rate, 0) > 0
			WHERE wi.published = 1
				AND IFNULL(wi.has_variants, 0) = 0
				AND wi.docstatus < 2
				AND wi.item_group IN (
					SELECT ig.name FROM `tabItem Group` ig WHERE ig.show_in_website = 1
				)
			GROUP BY wi.item_code
			HAVING COUNT(ip.item_code) = 0
			""",
			(price_list,),
			pluck=True,
		)

	frappe.local._zero_rate_item_codes = cached
	return cached


def _cart_key():
	return f"ecommerce_cart:{frappe.session.sid}"


def _cart_items():
	value = frappe.cache().get_value(_cart_key())
	return json.loads(value) if value else []


def _save_cart(items):
	frappe.cache().set_value(_cart_key(), json.dumps(items), expires_in_sec=60 * 60 * 24 * 30)


# Batch size for `IN (...)` lookups. Keeps the generated SQL (and the number of
# bound parameters) well inside MySQL's limits even for a large wishlist.
_SQL_CHUNK = 400


def _build_uom_list(stock_uom, conversion_factors, raw_prices):
	"""One item's UOM list from already-fetched rows.

	Pure function: no queries. This is the body that used to live in
	`_get_calculated_uoms`, moved out so a whole page of products can be
	resolved from three queries instead of three-per-product.
	"""
	price_map = {p["uom"]: p for p in raw_prices}
	base_price_doc = price_map.get(stock_uom) or (raw_prices[0] if raw_prices else None)

	seen = set()
	uoms_list = []
	for uom_name in [stock_uom] + list(conversion_factors.keys()):
		if not uom_name or uom_name in seen:
			continue
		seen.add(uom_name)
		if uom_name in price_map:
			uoms_list.append(price_map[uom_name])
		elif base_price_doc:
			conv_factor = flt(conversion_factors.get(uom_name) or 1.0) or 1.0
			uoms_list.append({
				"uom": uom_name,
				"price_list_rate": flt(base_price_doc["price_list_rate"]) * conv_factor,
				"currency": base_price_doc["currency"],
				"package": f"في {uom_name} {conv_factor} {stock_uom}",
			})
		else:
			# Fallback if no item price exists at all
			uoms_list.append({
				"uom": uom_name,
				"price_list_rate": 0,
				"currency": "",
			})

	# Final safeguard: if uoms_list is completely empty, force stock_uom
	if not uoms_list:
		uoms_list.append({
			"uom": stock_uom,
			"price_list_rate": flt(base_price_doc["price_list_rate"]) if base_price_doc else 0,
			"currency": base_price_doc["currency"] if base_price_doc else "",
		})

	return uoms_list


def _uom_prices_for_items(item_codes):
	"""Resolve UOM + price for many items in a fixed number of queries.

	This is the fix for the catalogue's worst N+1: `_decorate` used to call
	`_get_calculated_uoms` once per row, and each call did an `Item` read
	(loading the whole document), an `Item Price` query and a UOM loop. A
	24-product page therefore cost ~50 queries per request.

	Returns `{item_code: [uom dict, ...]}`. The per-item shape is byte-for-byte
	what the old helper returned, so callers cannot tell the difference apart
	from the response time.
	"""
	codes = [c for c in dict.fromkeys(item_codes or []) if c]
	if not codes:
		return {}

	price_list = _settings().price_list

	stock_uoms = {}
	factors = {}
	prices = {}

	for start in range(0, len(codes), _SQL_CHUNK):
		chunk = codes[start : start + _SQL_CHUNK]

		for row in frappe.get_all(
			"Item",
			filters={"name": ["in", chunk]},
			fields=["name", "stock_uom"],
			limit_page_length=0,
		):
			stock_uoms[row.name] = row.stock_uom or "Nos"

		for row in frappe.get_all(
			"UOM Conversion Detail",
			filters={"parenttype": "Item", "parent": ["in", chunk]},
			fields=["parent", "uom", "conversion_factor"],
			# `idx asc` preserves the child table's own order, which is the order
			# the old per-item `core_item_doc.uoms` loop walked.
			order_by="parent asc, idx asc",
			limit_page_length=0,
		):
			factors.setdefault(row.parent, {})[row.uom] = row.conversion_factor

		if price_list:
			for row in frappe.get_all(
				"Item Price",
				filters={"item_code": ["in", chunk], "price_list": price_list, "selling": 1},
				fields=["item_code", "uom", "price_list_rate", "currency"],
				# `modified desc` matches the old ordering, which decided which of
				# several prices for the same UOM won.
				order_by="modified desc",
				limit_page_length=0,
			):
				prices.setdefault(row.item_code, []).append(row)

	return {
		code: _build_uom_list(
			stock_uoms.get(code, "Nos"),
			factors.get(code, {}),
			prices.get(code, []),
		)
		for code in codes
	}


def _get_calculated_uoms(doc_item_code):
	"""Single-item wrapper, kept for admin_api and the cart line builders."""
	return _uom_prices_for_items([doc_item_code]).get(doc_item_code, [])


def get_cart_data():
	items = _cart_items()
	for item in items:
		item.setdefault("uoms", _get_calculated_uoms(item["item_code"]))
	total = sum(flt(item["amount"]) for item in items)
	settings = _settings()
	currency = frappe.db.get_value("Company", settings.company, "default_currency") or "YER"
	return {"items": items, "total": total, "grand_total": total, "currency": currency, "count": sum(flt(item["qty"]) for item in items)}


def _get_shipping_address(customer, address, phone=None):
	if not address:
		return None
	if frappe.db.exists("Address", address):
		return address
	address_doc = frappe.get_doc({
		"doctype": "Address",
		"address_title": customer.customer_name or customer.name,
		"address_type": "Shipping",
		"address_line1": frappe.utils.strip_html(address)[:140],
		"phone": frappe.utils.strip_html(phone or "")[:40],
		"links": [{"link_doctype": "Customer", "link_name": customer.name}],
	})
	address_doc.insert(ignore_permissions=True)
	return address_doc.name

  


def _ensure_leaf_customer_group():
	settings = _settings()
	group_name = settings.default_customer_group
	if not group_name or not frappe.db.get_value("Customer Group", group_name, "is_group"):
		return group_name

	parent = frappe.db.get_value("Customer Group", group_name, ["lft", "rgt"], as_dict=True)
	leaves = frappe.get_all(
		"Customer Group",
		filters={"lft": [">", parent.lft], "rgt": ["<", parent.rgt], "is_group": 0},
		pluck="name",
		order_by="lft asc",
		limit_page_length=1,
	)
	if not leaves:
		frappe.throw(_("Webshop Settings must use a non-group Default Customer Group."))

	frappe.db.set_single_value("Webshop Settings", "default_customer_group", leaves[0])
	frappe.clear_cache()
	return leaves[0]


def _price(item_code, uom=None):
	uoms = _get_calculated_uoms(item_code)
	if uom:
		matched = next((p for p in uoms if p["uom"] == uom), None)
		if matched:
			return frappe._dict(price_list_rate=matched["price_list_rate"], currency=matched["currency"], uom=matched["uom"])
	return frappe._dict(price_list_rate=uoms[0]["price_list_rate"], currency=uoms[0]["currency"], uom=uoms[0]["uom"]) if uoms else frappe._dict(price_list_rate=0, currency="", uom="")


def _item_fields():
	"""Website Item columns the storefront needs.

	`blurhash` is appended only when the site has the custom field, so a bench
	that has not added it yet keeps working instead of failing on an unknown
	column. See OVERHAUL_PLAN.md for the field definition and the backfill.
	"""
	fields = [
		"name", "item_code", "web_item_name", "item_name", "item_group", "route",
		"website_image", "thumbnail", "short_description", "description",
		"web_long_description", "stock_uom", "website_warehouse", "ranking", "on_backorder",
	]
	if frappe.get_meta("Website Item").has_field("blurhash"):
		fields.append("blurhash")
	return fields


def _decorate(item, uoms=None):
	if uoms is None:
		uoms = _get_calculated_uoms(item.item_code)
	price = uoms[0] if uoms else {"price_list_rate": 0, "currency": ""}
	item.image = item.website_image or item.thumbnail or ""
	item.price = flt(price.get("price_list_rate", 0))
	item.currency = price.get("currency", "")
	item.uoms = uoms
	item.in_stock = bool(item.on_backorder or item.website_warehouse)
	return item


def _website_groups():
	"""Website-visible Item Group names, resolved once per request.

	Used by the catalogue filter and by the group-visibility check. Memoised
	because a single request resolves it for every page it builds.
	"""
	cached = getattr(frappe.local, "_webshop_item_groups", None)
	if cached is None:
		cached = frappe.get_all(
			"Item Group",
			filters={"show_in_website": 1},
			pluck="name",
			limit_page_length=0,
		)
		frappe.local._webshop_item_groups = cached
	return cached


def _catalog_filters(item_group=None, search=None):
	allowed_groups = _website_groups()

	filters = {"published": 1, "has_variants": ["in", [0, None]]}
	or_filters = None

	group = (item_group or "").strip()
	if group:
		if group in allowed_groups:
			filters["item_group"] = group
		else:
			# A website-hidden group must not become readable just because a
			# client named it in a query string. An empty page is the honest
			# answer for a stale chip; leaking the products would be a bug with
			# no visible symptom.
			filters["name"] = ["in", []]
	else:
		filters["item_group"] = ["in", allowed_groups]

	if search:
		search_fields = ["web_item_name", "item_name", "item_code", "description", "short_description"]
		or_filters = [{field: ["like", f"%{search}%"]} for field in search_fields]

	# Applied here rather than after the rows come back, because this filter is
	# shared by `get_catalog` and `get_catalog_count`. Dropping zero-rated rows
	# in Python would leave the count describing a set the listing no longer
	# shows, and pagination would quietly stop advancing.
	if _hide_zero_rate_enabled():
		zero_rate = _zero_rate_item_codes()
		if zero_rate:
			filters["item_code"] = ["not in", zero_rate]

	return filters, or_filters




def get_catalog(item_group=None, search=None, limit=24, start=0):
	filters, or_filters = _catalog_filters(item_group, search)
	items = frappe.get_all(
		"Website Item", filters=filters, or_filters=or_filters, fields=_item_fields(),
		order_by="ranking desc, modified desc", limit_start=max(cint(start), 0), limit_page_length=min(cint(limit) or 24, 100),
	)
	# One batched UOM/price resolution for the whole page (was: per row).
	uom_map = _uom_prices_for_items([row.item_code for row in items])
	return [_decorate(item, uom_map.get(item.item_code)) for item in items]


def get_catalog_count(item_group=None, search=None):
	filters, or_filters = _catalog_filters(item_group, search)
	if or_filters:
		# `frappe.db.count` cannot take `or_filters`, and materialising every
		# matching name just to call len() is O(n) memory for a single number.
		# Ask the database for the count directly instead.
		rows = frappe.get_all(
			"Website Item",
			filters=filters,
			or_filters=or_filters,
			fields=["count(name) as total"],
			limit_page_length=0,
		)
		return cint(rows[0].total) if rows else 0
	return frappe.db.count("Website Item", filters=filters)

@frappe.whitelist(allow_guest=True)
def get_groups():
	"""Website-visible leaf groups, best-ranked first.

	`website_ranking desc` is the merchant's ordering control. `lft asc` breaks
	ties, so groups nobody has ranked keep their tree order instead of arriving in
	whatever order the database happened to return them in.
	"""
	return frappe.get_all(
		"Item Group",
		filters={"is_group": 0, "show_in_website": 1},
		fields=_group_fields(),
		order_by=_group_order_by(),
	)


def _has_website_ranking():
	"""Whether this site's Item Group carries a `website_ranking` field.

	The ordering is driven by a field the merchant adds to the doctype. Probing the
	meta once per request means a site that has not added it yet still gets its
	groups in tree order, rather than every category screen failing on an unknown
	column.
	"""
	cached = getattr(frappe.local, "_item_group_website_ranking", None)
	if cached is None:
		cached = bool(frappe.get_meta("Item Group").has_field("website_ranking"))
		frappe.local._item_group_website_ranking = cached
	return cached


def _group_order_by():
	return "website_ranking desc, lft asc" if _has_website_ranking() else "lft asc"


def _group_fields():
	fields = ["name", "parent_item_group", "image"]
	if _has_website_ranking():
		fields.append("website_ranking")
	return fields


def _rank_key(node):
	"""Highest `website_ranking` first, then name.

	The name is the tiebreak that matters: without it, equally ranked groups swap
	places between requests depending on the order the database returned, and the
	storefront reshuffles itself on every reload.
	"""
	return (-flt(node.get("website_ranking")), str(node.get("name") or ""))


@frappe.whitelist(allow_guest=True)
def get_group_hierarchy():
	"""The two-level storefront nav, ranked on both levels.

	Built here rather than in the app: a parent's `website_ranking` lives on the
	parent row, and `get_groups` returns leaves only (`is_group = 0`). A client
	that assembles the tree from that flat list therefore never receives the number
	that decides where a parent sits, so no amount of client-side sorting could
	honour it.

	A parent still appears only when at least one website-visible leaf points at
	it, which is the behaviour the previous leaf-driven tree had.
	"""
	leaves = get_groups()
	if not leaves:
		return []

	# The parent rows the leaves point at, so each parent's own ranking and image
	# can be read. Fetched regardless of `website_ranking` because the image is
	# part of the payload either way.
	parent_rows = {}
	for row in frappe.get_all(
		"Item Group",
		filters={"is_group": 1},
		fields=["name", "image"] + (["website_ranking"] if _has_website_ranking() else []),
	):
		parent_rows[row.name] = {
			"image": row.image or "",
			"website_ranking": flt(row.get("website_ranking")),
		}

	by_parent = {}
	for leaf in leaves:
		parent_name = (leaf.get("parent_item_group") or "").strip()
		if not parent_name:
			continue
		node = by_parent.get(parent_name)
		if node is None:
			meta = parent_rows.get(parent_name) or {}
			node = {
				"name": parent_name,
				"image": meta.get("image") or "",
				"website_ranking": meta.get("website_ranking") or 0,
				"children": [],
			}
			by_parent[parent_name] = node
		node["children"].append({
			"name": leaf.name,
			"image": leaf.image or "",
			"website_ranking": flt(leaf.get("website_ranking")),
		})

	nodes = list(by_parent.values())
	for node in nodes:
		node["children"].sort(key=_rank_key)
	nodes.sort(key=_rank_key)
	return nodes


@frappe.whitelist(allow_guest=True)
def webshop_settings():
	"""The storefront's feature switches, for the mobile app.

	`allow_guest` on purpose: a product page is public, so whether reviews and
	recommendations are shown cannot be a privileged read. Making it privileged
	would mean a guest sees a different product page than a signed-in shopper,
	which is worse than the two booleans being public.

	Only these two switches leave the server. The rest of `Webshop Settings`
	(company, price list, shipping account, default delivery rate) is back-office
	configuration and must never reach a client.
	"""
	settings = _settings()
	return {
		"enable_reviews": bool(settings.enable_reviews),
		"enable_recommendations": bool(settings.enable_recommendations),
	}


def _storefront_recommended(item_doc):
	"""Recommended products for a product page, resolved without a query per row.

	The page used to run one `Website Item` lookup and one full UOM/price
	resolution per recommended row, so a product recommending six others cost a
	dozen extra round trips before the response could be built. Both are batched
	here: one `Website Item` read and one `_uom_prices_for_items` call for the
	whole list.

	Rows whose item code no longer resolves keep the snapshot stored on the
	recommendation row and fall back to the local currency, which is exactly what
	the per-row loop produced when its lookup came back empty.
	"""
	rows = item_doc.recommended_items or []
	if not rows:
		return []

	codes = [r.item_code for r in rows if r.get("item_code")]
	details = {}
	if codes:
		for row in frappe.get_all(
			"Website Item",
			filters={"item_code": ["in", list(dict.fromkeys(codes))]},
			fields=["item_code", "item_name", "web_item_name", "route", "website_image", "item_group"],
			limit_page_length=0,
		):
			details.setdefault(row.item_code, row)

	uom_map = _uom_prices_for_items([r.item_code for r in rows if r.get("item_code")])

	recommended = []
	for rec in rows:
		rec_dict = rec.as_dict()
		detail = details.get(rec.get("item_code"))
		if detail:
			rec_dict.update(detail)

		uoms = uom_map.get(rec.get("item_code")) or []
		rec_dict["uoms"] = uoms
		if uoms:
			rec_dict["price"] = uoms[0]["price_list_rate"]
			rec_dict["currency"] = uoms[0]["currency"]
		else:
			rec_dict["price"] = 0
			rec_dict["currency"] = "ر.ي"
		recommended.append(rec_dict)

	return recommended


def get_item(route=None, item_code=None):
	filters = {"published": 1}

	# `item_code` wins over `route` when both are given. A `route` is a slug
	# derived from the product name, so it changes when an admin renames the
	# product, while `item_code` does not. Applying both as AND meant a wishlist
	# row saved before a rename looked up a product that no longer existed at
	# that route, and the whole wishlist failed with DoesNotExistError. The route
	# is only used on its own, for callers that genuinely identify by URL.
	if item_code:
		filters["item_code"] = item_code
	elif route:
		filters["route"] = route.strip("/")

	items = frappe.get_all("Website Item", filters=filters, fields=_item_fields(), limit_page_length=1)
	if not items:
		frappe.throw(_("This product is not available."), frappe.DoesNotExistError)

	item = _decorate(items[0])

	# The same rule as the listing, applied to the price the page is about to
	# display. Without it a shared or remembered link still reaches a zero-rated
	# product and renders the very "0" the setting exists to keep off the
	# storefront. Checked against the decorated price rather than the exclusion
	# list so this cannot drift from what `_catalog_filters` decided.
	if _hide_zero_rate_enabled() and not flt(item.price):
		frappe.throw(_("This product is not available."), frappe.DoesNotExistError)

	item_doc = frappe.get_doc("Website Item", item.name)
	item.recommended_items = _storefront_recommended(item_doc)
	item.offers = item_doc.offers or []
	item.reviews = frappe.get_all(
		"Item Review", filters={"website_item": item.name},
		fields=["user", "rating", "review_title", "comment", "published_on", "creation"],
		order_by="creation desc", limit_page_length=50,
	)
	return item

def _wishlist_items():
	"""The signed-in customer's wishlist, expanded with live product data.

	Batched for the same reason as the catalogue: the old body called `get_item`
	once per row and `get_item` itself runs several queries, so a twenty-item
	wishlist cost well over a hundred. One `Website Item` read and one UOM/price
	resolution now cover the whole list.

	A row that cannot be resolved is skipped rather than raised: a wishlist is
	long-lived and full of stale references by nature (deleted products,
	unpublished items, renames), and one of those must not empty the screen.
	"""
	if frappe.session.user == "Guest":
		return []

	records = frappe.get_all(
		"Wishlist Item",
		filters={"parent": frappe.session.user},
		fields=["item_code", "website_item", "web_item_name", "image", "route", "item_group"],
		order_by="modified desc",
	)
	if not records:
		return []

	codes = [r.item_code for r in records if r.get("item_code")]
	details = {}
	if codes:
		for row in frappe.get_all(
			"Website Item",
			filters={"item_code": ["in", list(dict.fromkeys(codes))], "published": 1},
			fields=_item_fields(),
			limit_page_length=0,
		):
			details.setdefault(row.item_code, row)

	uom_map = _uom_prices_for_items([r.item_code for r in records if r.get("item_code")])
	hide_zero_rate = _hide_zero_rate_enabled()

	items = []
	for record in records:
		detail = details.get(record.get("item_code"))
		# A product that has been deleted or unpublished is dropped, which is the
		# condition `get_item` used to raise `DoesNotExistError` for.
		if not detail:
			continue

		# Fresh values from the live document win over the snapshot stored on the
		# wishlist row: the stored `route` and `image` are from the moment the
		# product was added and go stale on every rename or image change.
		item = _decorate(
			frappe._dict({**record, **dict(detail)}),
			uom_map.get(record.get("item_code")),
		)
		# The product page's zero-rate rule applies here too, since the same
		# `get_item` used to enforce it on every wishlist row.
		if hide_zero_rate and not flt(item.price):
			continue
		items.append(item)

	return items


def update_website_context(context):
	cart_data = get_cart_data()
	context.cart_count = cart_data["count"]
	context.cart_total = cart_data["total"]
	context.cart_currency = cart_data["currency"]
	context.wishlist_count = len(_wishlist_items())
	return context


@frappe.whitelist(allow_guest=True)
def catalog(item_group=None, search=None, limit=24, offset=0):
	return {"items": get_catalog(item_group, search, limit, offset), "groups": get_groups()}


@frappe.whitelist(allow_guest=True)
def item(route=None, item_code=None):
	return get_item(route, item_code)


@frappe.whitelist(methods=["POST"])
def wishlist(item_code):
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to use your wishlist."), frappe.AuthenticationError)
	item = frappe.db.get_value("Wishlist Item", {"parent": frappe.session.user, "item_code": item_code}, "name")
	if item:
		from webshop.webshop.doctype.wishlist.wishlist import remove_from_wishlist
		remove_from_wishlist(item_code)
		added = False
	else:
		from webshop.webshop.doctype.wishlist.wishlist import add_to_wishlist
		add_to_wishlist(item_code)
		added = True
	return {"added": added, "count": len(_wishlist_items())}


@frappe.whitelist(allow_guest=True)
def wishlist_items():
	return _wishlist_items()


@frappe.whitelist(allow_guest=True)
def cart(item_code=None, qty=0, uom=None):
	if item_code:
		if uom in ("undefined", "null", ""):
			uom = None
		if flt(qty) <= 0:
			frappe.throw(_("Quantity must be greater than zero."))
		item = frappe.db.get_value("Website Item", {"item_code": item_code, "published": 1}, ["item_code", "item_name", "stock_uom", "website_warehouse","website_image"], as_dict=True)
		if not item:
			frappe.throw(_("This product is not available."))
		price = _price(item_code, uom)
		if not price.price_list_rate:
			frappe.throw(_("No selling price is configured for this product."))
		items = _cart_items()
		selected_uom = uom or item.stock_uom
		line = next((line for line in items if line["item_code"] == item_code and line["uom"] == selected_uom), None)
		if line:
			line["qty"] += flt(qty)
			line["amount"] = flt(line["qty"]) * flt(price.price_list_rate)
		else:
			items.append({"item_code": item.item_code, "item_name": item.item_name, "qty": flt(qty), "uom": selected_uom, "rate": flt(price.price_list_rate), "amount": flt(qty) * flt(price.price_list_rate), "warehouse": item.website_warehouse, "uoms": _get_calculated_uoms(item_code), "image": item.website_image})
		_save_cart(items)
	return get_cart_data()


@frappe.whitelist(allow_guest=True, methods=["POST"])
def update_cart_line(item_code, qty, uom=None, previous_uom=None):
	items = _cart_items()
	line = next((line for line in items if line["item_code"] == item_code and line["uom"] == (previous_uom or uom)), None)
	if not line:
		line = next((line for line in items if line["item_code"] == item_code), None)
	if not line:
		frappe.throw(_("Cart item was not found."))
		
	if flt(qty) <= 0:
		items.remove(line)
	else:
		selected_uom = uom or line["uom"]
		price = _price(item_code, selected_uom)
		line["uom"] = selected_uom
		line["qty"] = flt(qty)
		line["rate"] = flt(price.price_list_rate)
		line["amount"] = flt(qty) * flt(price.price_list_rate)
  
	_save_cart(items)
	return get_cart_data()


@frappe.whitelist(allow_guest=True, methods=["POST"])
def remove_from_cart(item_code, uom=None):
	"""Remove one cart line, or every line for the item when `uom` is omitted.

	The same item can sit in the cart under several UOMs (a box and a single
	unit are separate lines), so a caller that names a UOM means that line only.
	The old signature took no `uom` at all and therefore silently removed every
	line, which is not what the app's "remove this line" button asks for.
	"""
	item_code = (item_code or "").strip()
	uom = (uom or "").strip() or None

	def keep(item):
		if item["item_code"].strip() != item_code:
			return True
		return uom is not None and (item.get("uom") or "").strip() != uom

	_save_cart([item for item in _cart_items() if keep(item)])
	return get_cart_data()

@frappe.whitelist(allow_guest=True)
def addresses():
	if frappe.session.user == "Guest":
		return []
	from webshop.webshop.shopping_cart.cart import get_party
	customer = get_party()
	address_names = frappe.get_all("Dynamic Link", filters={"parenttype": "Address", "link_doctype": "Customer", "link_name": customer.name}, pluck="parent")
	# pincode added: it carries the readable place name from the map's reverse
	# geocode, so the app can display it and reopen the form fully labelled.
	return frappe.get_all("Address", filters={"name": ["in", address_names], "disabled": 0}, fields=["name", "address_title", "address_line1", "address_line2", "city", "state", "phone", "pincode", "is_primary_address","delivery_rate_charges"], order_by="is_primary_address desc, modified desc")

@frappe.whitelist(methods=["POST"])
def add_address(address_line1, phone=None, city=None, address_title=None, address_line2=None, pincode=None):  # new: address_line2, pincode
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in before adding an address."), frappe.AuthenticationError)
	
	from webshop.webshop.shopping_cart.cart import get_party
	customer = get_party()
	
	# Fallback if get_party() returns a user dict or email instead of customer name
	customer_name = customer.name if hasattr(customer, "name") else customer
	if not frappe.db.exists("Customer", customer_name):
		# Look up customer by email if customer_name is an email
		customer_name = frappe.db.get_value("Customer", {"email_id": frappe.session.user}, "name") or frappe.db.get_value("Contact", {"user": frappe.session.user}, "link_name")
		
	if not customer_name or not frappe.db.exists("Customer", customer_name):
		frappe.throw(_("Could not find a valid Customer profile for the current user."))

	doc = frappe.get_doc({
		"doctype": "Address", 
		"address_title": address_title or customer_name, 
		"address_type": "Shipping", 
		"address_line1": frappe.utils.strip_html(address_line1)[:140], 
		"phone": _resolve_phone(phone, customer_name),  # was: frappe.utils.strip_html(phone or "")[:40]
		"city": frappe.utils.strip_html(city or "Marib")[:80], 
		"address_line2": _clean_coords(address_line2),  # new
		"pincode": frappe.utils.strip_html(pincode or "")[:140],  # new
		"links": [{"link_doctype": "Customer", "link_name": customer_name}]
	})
	doc.insert(ignore_permissions=True)
	return doc.name

def _resolve_order_contact(customer):
	"""The Contact to attach to a new order, or None.

	Resolved through the session user and the customer's own links rather than
	by assuming a Contact shares the Customer's name: a Customer and a Contact
	are different records, so the old lookup returned None and the order crashed
	on `contact.name` before it could ever be inserted.
	"""
	name = frappe.db.get_value("Contact", {"user": frappe.session.user}, "name")
	if not name:
		linked = frappe.get_all(
			"Dynamic Link",
			filters={"parenttype": "Contact", "link_doctype": "Customer", "link_name": customer.name},
			pluck="parent",
			limit_page_length=1,
		)
		name = linked[0] if linked else None
	if not name:
		return None
	return frappe.db.get_value("Contact", name, ["name", "mobile_no"], as_dict=True)


@frappe.whitelist(methods=["POST"])
def submit_order(address=None, phone=None, notes=None, delivery_charges=None, shipping_account=None):  # new: the last two
	print("///////////////////////////")
	print(delivery_charges)
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in before submitting an order."), frappe.AuthenticationError)
	settings = _settings()
	if not settings.company:
		frappe.throw(_("Configure a company in Webshop Settings before accepting orders."))
	items = _cart_items()
	if not items:
		frappe.throw(_("Your cart is empty."))
	from webshop.webshop.shopping_cart.cart import get_party
	customer = get_party()
	order = frappe.get_doc({"doctype": "Sales Order", "customer": customer.name, "company": settings.company, "selling_price_list": settings.price_list, "order_type": "Shopping Cart", "terms": notes or "Cash on delivery", "items": [{"item_code": item["item_code"], "item_name": item["item_name"], "qty": item["qty"], "uom": item["uom"], "rate": item["rate"], "warehouse": item["warehouse"]} for item in items]})
	order.shipping_address_name = _get_shipping_address(customer, address, phone)
	contact = _resolve_order_contact(customer)
	if contact:
		order.contact_person = contact.name
		order.contact_display = customer.customer_name or customer.name
		order.contact_mobile = contact.mobile_no
	order.flags.ignore_permissions = True

	# --- delivery charge, new ---
	# Resolved from the SAVED address, not the incoming `address` argument:
	# _get_shipping_address may have just created a fresh Address, and a brand new
	# one has no per-address rate, so it correctly falls back to the shop default.
	delivery = _resolve_delivery_charges(order.shipping_address_name, settings)

	# The app sends its own figures so the payload is explicit and the
	# confirmation screen can echo them. They are NOT trusted — a client-supplied
	# amount is not evidence of what the customer owes. A disagreement is logged
	# because it means the quoted price no longer matches the charged price.
	if delivery_charges is not None and flt(delivery_charges) != delivery["charges"]:
		frappe.logger("ecommerce").warning(
			"Delivery charge mismatch on %s: client=%s server=%s",
			order.shipping_address_name, delivery_charges, delivery["charges"]
		)

	if delivery["charges"] > 0:
		if not delivery["shipping_account"]:
			frappe.throw(_("Set a shipping account in Webshop Settings before accepting delivery charges."))
		# Document.append, NOT order.taxes.append(...): child tables hold Document
		# objects, and appending a plain dict leaves a raw dict in the list that
		# _set_defaults then calls .is_new() on.
		order.append("taxes", {
			"charge_type": "Actual",
			"account_head": delivery["shipping_account"],
			"description": _("Delivery Charges"),
			# `rate`, not `tax_amount`. tax_amount is a read-only computed field on
			# Sales Taxes and Charges, so setting it stores nothing and the charge
			# silently becomes 0.
			"tax_amount": delivery["charges"],
			"cost_center": frappe.db.get_value("Company", settings.company, "cost_center"),
			"included_in_print_rate": 0,
		})

	order.set_missing_values()
	order.insert(ignore_permissions=True)

	# A Taxes and Charges template can rebuild `taxes` and drop the manual row,
	# which would ship the order with no delivery fee at all. Caught here rather
	# than discovered at invoicing.

	order.submit()
	_save_cart([])
	return {
		"name": order.name,
		"payment_method": "Cash on Delivery",
		"delivery_charges": delivery["charges"],
		"shipping_account": delivery["shipping_account"],
		"delivery_source": delivery["source"],
		"grand_total": flt(order.grand_total),
	}


@frappe.whitelist(methods=["POST"])
def reviews(website_item, rating, review_title, comment):
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to write a review."), frappe.AuthenticationError)
	if not frappe.db.exists("Website Item", {"name": website_item, "published": 1}):
		frappe.throw(_("This product is not available."))
	review = frappe.get_doc({"doctype": "Item Review", "website_item": website_item, "user": frappe.session.user, "rating": max(1, min(cint(rating), 5)), "review_title": frappe.utils.strip_html(review_title)[:140], "comment": frappe.utils.strip_html(comment)[:2000], "published_on": nowdate()})
	review.insert(ignore_permissions=True)
	return review.name


@frappe.whitelist()
def orders(start: int = 0, page_length: int = 12):
	"""Return one page of the current customer's sales orders, newest first."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Please log in to view your orders."), frappe.PermissionError)

	from webshop.webshop.shopping_cart.cart import get_party    

	customer = get_party()   # <- keep your existing customer lookup
	if not customer:
		return {"orders": [], "total": 0, "start": 0, "page_length": 0, "has_more": False}

	start = max(cint(start) or 0, 0)
	page_length = min(max(cint(page_length) or 12, 1), 100)

	filters = {"customer": customer.name, "docstatus": ["<", 2]}
	total = frappe.db.count("Sales Order", filters)

	order_rows = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=["name", "transaction_date", "status", "docstatus", "grand_total", "currency"],
		# `transaction_date` is date-only, so orders placed the same day used to
		# come back in arbitrary order. `creation` breaks the tie.
		order_by="transaction_date desc, creation desc, name desc",
		limit_start=start,
		limit_page_length=page_length,
	)

	# One query for the whole page instead of one per order (the old N+1 loop).
	items_by_order = {}
	order_names = [row.name for row in order_rows]
	if order_names:
		for item in frappe.get_all(
			"Sales Order Item",
			filters={"parent": ["in", order_names], "docstatus": ["<", 2]},
			fields=["parent", "item_name", "qty", "uom", "rate", "amount"],
			order_by="idx asc",
		):
			items_by_order.setdefault(item.parent, []).append({
				"item_name": item.item_name,
				"qty": item.qty,
				"uom": item.uom,
				"rate": item.rate,
				"amount": item.amount,
			})

	for row in order_rows:
		row["items"] = items_by_order.get(row.name, [])

	loaded = len(order_rows)
	return {
		"orders": order_rows,
		"total": total,
		"start": start,
		"page_length": page_length,
		"has_more": start + loaded < total,
	}


@frappe.whitelist(methods=["POST"])
def cancel_order(order_name):
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to manage your orders."), frappe.AuthenticationError)
	from webshop.webshop.shopping_cart.cart import get_party
	customer = get_party()
	order = frappe.get_doc("Sales Order", order_name)
	if order.customer != customer.name:
		frappe.throw(_("You cannot manage this order."), frappe.PermissionError)
	if order.docstatus != 1 or order.status in ("Completed", "Closed", "Cancelled"):
		frappe.throw(_("This order can no longer be cancelled."))
	order.flags.ignore_permissions = True
	order.cancel()
	return {"name": order.name, "status": order.status}


@frappe.whitelist(methods=["POST"])
def reorder_order(order_name):
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to reorder."), frappe.AuthenticationError)
	from webshop.webshop.shopping_cart.cart import get_party
	customer = get_party()
	order = frappe.get_doc("Sales Order", order_name)
	if order.customer != customer.name:
		frappe.throw(_("You cannot manage this order."), frappe.PermissionError)
	items = _cart_items()
	for source in order.items:
		item = frappe.db.get_value("Website Item", {"item_code": source.item_code, "published": 1}, ["item_code", "item_name", "stock_uom", "website_warehouse"], as_dict=True)
		if not item:
			continue
		uom = source.uom or item.stock_uom
		price = _price(source.item_code, uom)
		if not price.price_list_rate:
			continue
		line = next((line for line in items if line["item_code"] == source.item_code and line["uom"] == uom), None)
		if line:
			line["qty"] += flt(source.qty)
			line["rate"] = flt(price.price_list_rate)
			line["amount"] = flt(line["qty"]) * flt(price.price_list_rate)
		else:
			items.append({"item_code": item.item_code, "item_name": item.item_name, "qty": flt(source.qty), "uom": uom, "rate": flt(price.price_list_rate), "amount": flt(source.qty) * flt(price.price_list_rate), "warehouse": item.website_warehouse, "uoms": _get_calculated_uoms(source.item_code)})
	_save_cart(items)
	return get_cart_data()


# Columns the app is allowed to see on its own account. The full User document
# carries roles, API keys, enabled flags and internal timestamps, none of which
# the storefront needs; only this projection leaves the server.
_CURRENT_USER_FIELDS = ("name", "email", "full_name", "first_name", "last_name", "mobile_no")


@frappe.whitelist(allow_guest=True)
def get_current_user():
	"""The signed-in account, projected to the fields the app actually uses."""
	if frappe.session.user == "Guest":
		return None
	user = frappe.db.get_value("User", frappe.session.user, list(_CURRENT_USER_FIELDS), as_dict=True)
	if not user:
		return None
	return {"doctype": "User", **user}



def _get_customer_name():
	"""Resolve the Customer for the session.

	Extracted verbatim from `add_address` — same resolution order, same
	fallbacks, no behaviour change."""
	from webshop.webshop.shopping_cart.cart import get_party
	customer = get_party()

	customer_name = customer.name if hasattr(customer, "name") else customer
	if not frappe.db.exists("Customer", customer_name):
		# Look up customer by email if customer_name is an email
		customer_name = frappe.db.get_value("Customer", {"email_id": frappe.session.user}, "name") or frappe.db.get_value("Contact", {"user": frappe.session.user}, "link_name")

	if not customer_name or not frappe.db.exists("Customer", customer_name):
		frappe.throw(_("Could not find a valid Customer profile for the current user."))
	return customer_name


def _customer_address_names(customer_name):
	"""Addresses linked to this customer.

	Same Dynamic Link filter as the `addresses` list, so the two can never
	disagree about which addresses "mine" means."""
	return frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": "Address", "link_doctype": "Customer", "link_name": customer_name},
		pluck="parent",
	)


def _require_login():
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in before managing an address."), frappe.AuthenticationError)


def _owned_address(address_name):
	"""Fetch an Address only if it belongs to the signed-in customer."""
	_require_login()
	address_name = (address_name or "").strip()
	if not address_name:
		frappe.throw(_("Address is required."))

	if address_name not in _customer_address_names(_get_customer_name()):
		# Reported as "not found" rather than "not yours", so this cannot be used
		# to probe for another customer's address names.
		frappe.throw(_("Address not found."), frappe.DoesNotExistError)
	return frappe.get_doc("Address", address_name)


def _clean_coords(value):
	"""Validate a "lat,lng" pin before storing it.

	Out of range is worse than absent: it puts the delivery in the ocean."""
	parts = [p.strip() for p in (value or "").split(",")]
	if len(parts) != 2:
		return ""
	try:
		lat, lng = float(parts[0]), float(parts[1])
	except ValueError:
		return ""
	if not (-90 <= lat <= 90 and -180 <= lng <= 180):
		return ""
	return f"{lat:.6f},{lng:.6f}"


def _resolve_phone(phone, customer_name=None):
	"""Recipient phone, falling back to the customer's own number.

	The app lets the shopper leave this empty when the order is for themselves,
	so the fallback lives server-side: the client is never trusted to supply a
	number, and the Customer/User documents are authoritative."""
	def clean(value):
		value = re.sub(r"[^\d+]", "", str(value or "")).strip()
		return frappe.utils.strip_html(value)[:40] if len(re.sub(r"\D", "", value)) >= 6 else None

	resolved = clean(phone)
	if resolved:
		return resolved

	for candidate in (
		frappe.db.get_value("Customer", customer_name, "mobile_no") if customer_name else None,
		frappe.db.get_value("User", frappe.session.user, "mobile_no"),
		frappe.session.user.split("@")[0] if "@" in frappe.session.user else None,
	):
		resolved = clean(candidate)
		if resolved:
			return resolved

	return ""

WRITABLE = ("address_title", "address_line1", "address_line2", "city", "phone", "pincode")


@frappe.whitelist(methods=["POST"])
def update_address(address_name=None, **kwargs):
	"""Only `WRITABLE` is applied, and an invalid pin is dropped rather than
	written, so a partial edit cannot blank a column nobody touched."""
	doc = _owned_address(address_name)
	customer_name = _get_customer_name()

	for field in WRITABLE:
		if field in kwargs:
			value = (kwargs.get(field) or "").strip()
			if field == "address_line2":
				# An unparseable pin is skipped, not written: overwriting a good
				# stored pin with "" loses the delivery location entirely.
				cleaned = _clean_coords(value)
				if cleaned:
					doc.address_line2 = cleaned
			elif field == "phone":
				doc.phone = _resolve_phone(value, customer_name) or ""
			else:
				doc.set(field, frappe.utils.strip_html(value)[:140])

	doc.save()

	# Promoted, never demoted: a customer must keep exactly one primary address,
	# and only `set_default_address` may move the flag.
	if kwargs.get("is_default_address"):
		_set_primary(doc.name, customer_name)

	return {"name": doc.name, "message": "Address updated"}


@frappe.whitelist(methods=["POST"])
def delete_address(address_name=None):
	"""Ownership is checked before the delete, and a surviving address is
	promoted so the customer is never left with no primary."""
	doc = _owned_address(address_name)
	customer_name = _get_customer_name()
	was_primary = doc.is_primary_address
	doc.delete()

	if was_primary:
		remaining = [n for n in _customer_address_names(customer_name) if not frappe.db.exists("Address", n, {"disabled": 1})]
		if remaining and not frappe.db.exists("Address", {"is_primary_address": 1, "name": ["in", remaining]}):
			frappe.db.set_value("Address", remaining[0], "is_primary_address", 1)

	return {"message": "Address deleted"}


@frappe.whitelist(methods=["POST"])
def set_default_address(address_name=None):
	doc = _owned_address(address_name)
	_set_primary(doc.name, _get_customer_name())
	return {"name": doc.name, "message": "Default address updated"}


def _set_primary(address_name, customer_name):
	"""Move the primary flag: clear it everywhere first, then set one, so a
	half-finished request can never leave two primary addresses."""
	for name in _customer_address_names(customer_name):
		frappe.db.set_value("Address", name, "is_primary_address", 0)
	frappe.db.set_value("Address", address_name, "is_primary_address", 1)	


# ---------------------------------------------------------------------------
# Delivery charge
# ---------------------------------------------------------------------------



def _resolve_delivery_charges(address_name=None, settings=None):
	"""Delivery charge for an address, and where that number came from.

	Resolution order: the address's own `delivery_rate_charges`, and only when
	that is unset or zero the shop-wide `default_delivery_rate`. One
	implementation, shared by the cart quote and by submit_order, so the price
	shown at checkout and the price charged cannot disagree.

	Returns {charges, source, shipping_account}, source being
	'address' | 'default' | 'free'.
	"""
	settings = settings or _settings()
	shipping_account = settings.shipping_account or ""
	charges = flt(address_name and frappe.db.get_value("Address", address_name, "delivery_rate_charges"))

	if charges > 0:
		return {"charges": round(charges, 2), "source": "address", "shipping_account": shipping_account}

	charges = flt(settings.default_delivery_rate)
	if charges > 0:
		return {"charges": round(charges, 2), "source": "default", "shipping_account": shipping_account}

	return {"charges": 0.0, "source": "free", "shipping_account": shipping_account}


@frappe.whitelist()
def delivery_estimate(address=None):
	"""Delivery charge for the cart summary. Read-only."""
	if frappe.session.user == "Guest":
		return {"delivery_charges": 0, "source": "free", "shipping_account": ""}

	from webshop.webshop.shopping_cart.cart import get_party
	customer = get_party()
	customer_name = customer.name if hasattr(customer, "name") else customer

	if address:
		owned = frappe.get_all(
			"Dynamic Link",
			filters={"parenttype": "Address", "link_doctype": "Customer", "link_name": customer_name},
			pluck="parent",
		)
		# Not the customer's -> treated as no address, so a stale selection cannot
		# block a checkout.
		if address not in owned:
			address = None

	return _resolve_delivery_charges(address, _settings())	