"""Catalogue administration for the mobile app.

Every endpoint here is gated on the signed-in user appearing in the
`admin_users` child table of Webshop Settings. This module is the only place
that decides who is an admin: nothing is accepted from the client about
identity, and no endpoint trusts a value the client sends about permissions.

Complex arguments (UOM rows, recommended items) arrive as JSON *strings*, not
as nested arrays. Frappe calls whitelisted methods as `fn(**frappe.form_dict)`,
so the shape of a nested payload is only as stable as the HTTP parser in front
of it; a string is one value and cannot be half-parsed. The mobile app is the
only caller and encodes with `JSON.stringify` for the same reason.
"""

import base64
import io
import json

import frappe
from frappe import _
from frappe.utils import cint, flt, strip_html
import re
from webshop.my_api.api import _get_calculated_uoms, _settings
from frappe.utils.password import update_password

# Mirrors the storefront's own rule, so an item in a hidden group keeps behaving
# the way the website behaves.
MAX_IMAGE_EDGE = 800
IMAGE_QUALITY = 75
MAX_RAW_IMAGE_BYTES = 15 * 1024 * 1024


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------

def slugify(text):
	if not text:
		return ""
	text = text.lower()
	text = re.sub(r'[^a-z0-9\s-]', '', text) # Remove invalid chars
	text = re.sub(r'[\s-]+', '-', text).strip('-') # Collapse spaces/hyphens
	return text




@frappe.whitelist(allow_guest=True)
def register_user_by_phone(phone, full_name, new_password):
    generatedEmail = f"{phone}@yemen.local"
    
    # Check if user already exists by email or mobile number
    if frappe.db.exists("User", generatedEmail) or frappe.db.exists("User", {"mobile_no": phone}):
        return {"status": "error", "message": "هذا الحساب مسجل مسبقاً، يرجى تسجيل الدخول مباشرة."}
    
    try:
        # Create the user document
        user = frappe.get_doc({
            "doctype": "User",
            "email": generatedEmail,
            "first_name": full_name,
            "mobile_no": phone,
            "user_type": "Website User",
            "enabled": 1,
            "send_welcome_email": 0
        })
        user.insert(ignore_permissions=True)
        
        # Set the password
        update_password(generatedEmail, new_password)
        
        return {"status": "success", "message": "تم إنشاء الحساب بنجاح!"}
    
    except Exception as e:
        return {"status": "error", "message": str(e)}

def _admin_child_doctype():
	"""The child table behind Webshop Settings.admin_users.

	Read from the parent's meta rather than hardcoded to
	 "Webshop Admin Users", so renaming the child doctype cannot
	silently deny every admin in the shop.
	"""
	field = frappe.get_meta("Webshop Settings").get_field("admin_users")
	return field.options if field else None


def _admin_user_column():
	"""The Link-to-User fieldname inside the admin child table.

	Hardcoded to `admin_user`, which is that table's only field. It is still
	verified rather than assumed: querying a column that does not exist raises
	out of `frappe.get_all`, and the capability probe swallows that into
	"nobody is an admin". An explicit check turns a silent lockout into a
	readable misconfiguration message.
	"""
	meta = frappe.get_meta(_admin_child_doctype())
	if not meta.get_field("admin_user"):
		frappe.throw(
			_("`{0}` has no `admin_user` field.").format(_admin_child_doctype()),
			frappe.PermissionError,
		)
	return "admin_user"


def _is_admin():
	"""Is the session user an admin?

	Deliberately NOT cached: an admin removed from the table loses access on
	their next request rather than at the next app restart.
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		return False

	# The shop owner must never be locked out of their own catalogue by a
	# misconfigured table.
	if user == "Administrator":
		return True

	child = _admin_child_doctype()
	if not child:
		# Misconfiguration is raised, not swallowed. Returning False here would
		# present as "nobody is an admin" and the cause would be invisible from
		# the app.
		frappe.throw(
			_("Webshop Settings has no `admin_users` table. Add it before using admin tools."),
			frappe.PermissionError,
		)

	# Filtered in the query rather than pulled into Python: the table holds every
	# admin, and the client has no business receiving that list.
	column = _admin_user_column()
	return bool(
		frappe.get_all(
			child,
			filters={"parent": "Webshop Settings", column: user},
			pluck=column,
			limit_page_length=1,
		)
	)


def _require_admin():
	"""Gate for every mutating or catalogue-wide endpoint.

	Called first, before any argument is read, so an unauthorised caller cannot
	distinguish "item not found" from "not allowed" by timing or message.
	"""
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in first."), frappe.AuthenticationError)
	if not _is_admin():
		frappe.throw(_("You are not allowed to manage the catalogue."), frappe.PermissionError)


# ---------------------------------------------------------------------------
# Schema-tolerant reads
# ---------------------------------------------------------------------------


def _fields_that_exist(doctype, wanted):
	"""Drop fields the doctype does not actually have.

	`web_long_description`, `ranking` and `delivery_rate_charges` are custom
	fields. Selecting one that has not been created yet raises a 500 that looks
	like a bug in this module, so the read adapts to the schema instead.
	"""
	meta = frappe.get_meta(doctype)
	return [field for field in wanted if meta.has_field(field)]


def _get_one(doctype, name, wanted):
	fields = _fields_that_exist(doctype, wanted)
	row = frappe.db.get_value(doctype, name, fields, as_dict=True)
	return row or frappe._dict()


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------


def _decode_image(file_base64):
	"""Validate, de-rotate and downscale an uploaded image.

	Returns `(content, extension)`. The resize happens here rather than trusting
	the device to have sent something small: a 12MP phone photo is several
	megabytes of base64 on the wire and is a request the web server may reject
	before this code runs at all.
	"""
	from PIL import Image, ImageOps

	if not file_base64:
		frappe.throw(_("No image data was received."))

	# The app sends a data URL; keep the payload so the same endpoint works if a
	# caller sends raw base64.
	if "," in file_base64[:64] and file_base64.lstrip().startswith("data:"):
		file_base64 = file_base64.split(",", 1)[1]

	try:
		raw = base64.b64decode(file_base64, validate=True)
	except Exception:
		frappe.throw(_("The image data could not be decoded."))

	if not raw:
		frappe.throw(_("The image data was empty."))
	if len(raw) > MAX_RAW_IMAGE_BYTES:
		frappe.throw(_("That image is too large. Please use a smaller photo."))

	try:
		image = Image.open(io.BytesIO(raw))
		image.load()
	except Exception:
		frappe.throw(_("That file is not a readable image."))

	# Phone cameras store the rotation in EXIF rather than in the pixels. Without
	# this the product photo lands sideways in the storefront.
	image = ImageOps.exif_transpose(image)

	# Resize in place; `thumbnail` keeps the aspect ratio and never upscales.
	image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))

	# Flatten transparency: a product photo is shown on white, and JPEG has no
	# alpha channel, so keeping RGBA would either fail or render as black.
	if image.mode not in ("RGB", "L"):
		image = image.convert("RGB")

	buffer = io.BytesIO()
	image.save(buffer, format="WEBP", quality=IMAGE_QUALITY, optimize=True)
	return buffer.getvalue(), "webp"


def _delete_superseded_image(previous_url, item_code):
	"""Remove the old product photo, but only if this item is its sole owner.

	Deleting by URL alone would delete an image another item still points at —
	from a copy, a template, or a shared asset — and break that product silently.
	"""
	if not previous_url or "/files/" not in previous_url:
		return

	old_name = previous_url.rsplit("/", 1)[-1]
	if not old_name:
		return

	attached_to = frappe.db.get_value(
		"File",
		{"file_name": old_name, "attached_to_doctype": "Item", "attached_to_name": item_code},
		["name", "attached_to_doctype", "attached_to_name"],
		as_dict=True,
	)
	if not attached_to:
		# Either it was never attached to an Item (so it may be shared) or it is
		# already gone. Either way, leave it alone.
		return

	still_referenced = frappe.db.exists(
		"Website Item",
		{"website_image": ["like", f"%{old_name}%"], "name": ["!=", "_placeholder"]},
	)
	if still_referenced:
		return

	try:
		frappe.delete_doc("File", attached_to.name, ignore_permissions=True, force=True)
	except Exception:
		# A locked or already-removed file is not worth failing an edit over.
		frappe.log_error(title="Could not remove superseded item image")


from frappe.utils.file_manager import save_file

def _save_item_image(item_code, file_base64, file_name=None):
	"""Store the photo, attach it to Item.image and point Website Item at it.

	Both doctypes are written because they are read by different consumers: the
	website renders Website Item.website_image, while anything working from the
	master record reads Item.image.
	"""
	if not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item not found."), frappe.DoesNotExistError)

	content, extension = _decode_image(file_base64)

	website_item = frappe.db.get_value("Website Item", {"item_code": item_code}, "name")
	previous = frappe.db.get_value("Website Item", website_item, "website_image") if website_item else None

	stem = slugify((file_name or "").rsplit(".", 1)[0]) or _custom_slugify(item_code)
	filename = f"{stem}-{frappe.generate_hash(length=8)}.{extension}"

	# Call save_file imported from frappe.utils.file_manager
	file_doc = save_file(
		fname=filename,
		content=content,
		dt="Item",
		dn=item_code,
		folder="Home/Attachments",
		is_private=0,
		df="image"
	)

	if website_item:
		frappe.db.set_value("Website Item", website_item, "website_image", file_doc.file_url)
		frappe.db.set_value("Item", item_code, "image", file_doc.file_url)

	_delete_superseded_image(previous, item_code)
	return file_doc.file_url


# ---------------------------------------------------------------------------
# UOMs and prices
# ---------------------------------------------------------------------------


def _price_list():
	return _settings().price_list


def _item_price_row(item_code, uom):
	"""The explicit Item Price for this UOM, or None.

	`ecommerce.api._get_calculated_uoms` *derives* a price for any UOM without an
	Item Price row by multiplying the base price by the conversion factor. That
	is right for browsing and wrong for editing: the editor has to show which
	figures are stored and which are merely computed, or an admin edits a number
	the database does not contain.
	"""
	return frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": _price_list(), "uom": uom, "selling": 1},
		["name", "price_list_rate", "currency"],
		as_dict=True,
	)


def _uom_rows(item_code):
	"""Every UOM on the item, with its conversion factor and selling price.

	Reuses the storefront's price resolution so the editor shows exactly what a
	shopper would be charged for each unit, then flags whether that price is
	stored or derived.
	"""
	item = frappe.get_doc("Item", item_code)
	stock_uom = item.stock_uom
	# Read back unrounded: a stored 0.75 must reach the editor as 0.75, and the
	# field is only ever rounded by ERPNext at save time.
	factors = {row.uom: flt(row.conversion_factor) for row in (item.uoms or [])}

	rows = []
	for entry in _get_calculated_uoms(item_code):
		uom = entry.get("uom")
		if not uom:
			continue
		explicit = _item_price_row(item_code, uom)
		if uom == stock_uom:
			# The stock UOM is the conversion base, so its factor is always 1 by
			# definition. Reporting anything else would invite an admin to enter a
			# value ERPNext will reject on save.
			factor = 1.0
		else:
			factor = factors.get(uom)
			# No `or 1.0` here: that would hide a stored zero, and a unit with a
			# zero factor is broken data the admin should be shown, not hidden.
			factor = 1.0 if factor is None else factor

		rows.append(
			{
				"uom": uom,
				"conversion_factor": factor,
				"price_list_rate": flt(entry.get("price_list_rate")),
				"currency": entry.get("currency") or "",
				"has_item_price": 1 if explicit else 0,
				"is_stock_uom": 1 if uom == stock_uom else 0,
			}
		)
	return rows


_ARABIC_DIGIT_TABLE = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _decimal(value, precision=3):
	"""Parse a number sent by the app, keeping three decimal places.

	The app already normalises Arabic-Indic digits and the Arabic decimal
	separator before sending, but this is the last line of defence, and it matters:
	`flt("٠٫٧٥")` is `0`, so a perfectly good conversion factor could arrive as
	zero and be rejected as "must be greater than zero" with no clue why.

	Rounded to `precision` decimals so float noise does not leak into
	`UOM Conversion Detail`, while 0.75 stays 0.75 instead of being flattened.
	"""
	if value is None:
		return None

	if isinstance(value, str):
		text = value.translate(_ARABIC_DIGIT_TABLE).strip()
		if not text:
			return None
		# The Arabic decimal separator, and a comma when no dot has been typed.
		text = text.replace("٫", ".").replace("٬", "")
		if "." not in text:
			text = text.replace(",", ".")
		else:
			text = text.replace(",", "")
		value = text

	try:
		return flt(value, precision)
	except Exception:
		return None


def _float_precision_warning():
	"""Warn when the site's Float precision is too low for conversion factors.

	`UOM Conversion Detail.conversion_factor` is a Float field, and ERPNext rounds
	Float columns to the "Float Precision" in System Settings. At a precision of 1
	an entered 0.75 is stored and shown as 0.8, and an entered 0.0... well, at a
	precision of 1 anything below 0.05 rounds to 0.0 and is then rejected as a
	non-positive rate. The admin sees a number they never typed and no explanation,
	so this names the setting and the symptom instead of leaving them guessing.
	"""
	precision = cint(frappe.db.get_single_value("System Settings", "float_precision") or 3)
	if precision >= 3:
		return None

	return _(
		"System Settings > Float Precision is {0}, so conversion rates are rounded to {0} "
		"decimal place(s): 0.75 is stored as 0.8. Set Float Precision to at least 3."
	).format(precision)


def _apply_uoms(item_code, rows):
	"""Write UOM conversion factors and their selling prices.

	`rows` is the full desired list from the editor. Anything absent from it is
	removed, so deleting a row in the UI deletes it here too.
	"""
	item = frappe.get_doc("Item", item_code)
	stock_uom = item.stock_uom

	seen = set()
	for row in rows:
		uom = (row.get("uom") or "").strip()
		if not uom:
			continue
		if uom in seen:
			frappe.throw(_("Unit of Measure `{0}` is listed twice.").format(uom))
		seen.add(uom)

		if not frappe.db.exists("UOM", uom):
			frappe.throw(_("Unit of Measure `{0}` does not exist.").format(uom))

		existing = next((r for r in (item.uoms or []) if r.uom == uom), None)

		factor = _decimal(row.get("conversion_factor"))

		if uom == stock_uom:
			# Forced, not validated: the base unit has no conversion.
			factor = 1.0
		elif factor is None:
			# Absent means "leave the stored factor alone". This is the same rule
			# the price follows, and it matters because a blank field used to
			# arrive here as 0 and abort the entire save.
			if existing:
				continue
			frappe.throw(
				_("Conversion rate for `{0}` is required because it is a new unit.").format(uom)
			)
		elif factor <= 0:
			# The received value is quoted back: "must be greater than zero" on its
			# own gave no way to tell a genuinely wrong entry from a number mangled
			# somewhere upstream.
			frappe.throw(
				_("Conversion rate for `{0}` must be greater than zero (received `{1}`).").format(
					uom, row.get("conversion_factor")
				)
			)

		if existing:
			if factor is not None:
				existing.conversion_factor = factor
		else:
			# A new unit needs a positive factor. `UOM Conversion Detail` is a
			# precision field, so a fractional rate is legitimate.
			item.append("uoms", {"uom": uom, "conversion_factor": factor})

	# Remove units the editor dropped.
	for existing in list(item.uoms or []):
		if existing.uom != stock_uom and existing.uom not in seen:
			item.remove(existing)

	item.save()

	for row in rows:
		uom = (row.get("uom") or "").strip()
		if not uom:
			continue

		# Absent means "leave this unit's price alone", NOT "set it to zero".
		# `flt(None, 0)` would return 0 and silently overwrite a real price with a
		# free one, which is how a derived unit — a unit the app only shows as
		# computed information — could wipe the price the shop actually uses.
		if row.get("price_list_rate") is None:
			continue

		rate = flt(row.get("price_list_rate"), 0)
		if rate < 0:
			continue
		_apply_item_price(item_code, uom, rate)


def _apply_item_price(item_code, uom, rate):
	"""Upsert the selling price for one UOM in the webshop price list.

	Only the webshop price list is touched. This app is one sales channel;
	rewriting a wholesale or POS price list because someone edited a product on
	their phone would be destructive and is not what was asked for.
	"""
	price_list = _price_list()
	if not price_list:
		frappe.throw(_("No price list is configured in Webshop Settings."))

	existing = _item_price_row(item_code, uom)
	if existing:
		frappe.db.set_value("Item Price", existing.name, "price_list_rate", rate)
		return

	settings = _settings()
	company = settings.company
	currency = frappe.db.get_value("Company", company, "default_currency") if company else None

	frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": item_code,
			"price_list": price_list,
			"selling": 1,
			"uom": uom,
			"price_list_rate": rate,
			"currency": currency or "YER",
		}
	).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


def _reference_data():
	settings = _settings()
	company = settings.company

	# Leaf groups marked for the website. A parent group has no products of its
	# own, and a group hidden from the website would make the product vanish.
	item_groups = frappe.get_all(
		"Item Group",
		filters={"is_group": 0, "show_in_website": 1},
		pluck="name",
		order_by="lft asc",
		limit_page_length=0,
	)

	uom_list = frappe.get_all(
		"UOM", filters={"enabled": 1}, pluck="name", order_by="name asc", limit_page_length=0
	)

	warehouses = []
	if company:
		warehouses = frappe.get_all(
			"Warehouse",
			filters={"company": company, "is_group": 0, "disabled": 0},
			pluck="name",
			order_by="name asc",
			limit_page_length=0,
		)

	return {
		"item_groups": item_groups,
		"uom_list": uom_list,
		"warehouses": warehouses,
		"price_list": settings.price_list,
		"company": company,
		"currency": frappe.db.get_value("Company", company, "default_currency") if company else "YER",
		"website_warehouse": "مخازن - ن",
	}


def _parse_json_list(value, label):
	if not value:
		return []
	try:
		parsed = json.loads(value) if isinstance(value, str) else value
	except Exception:
		frappe.throw(_("`{0}` was not valid JSON.").format(label))
	if not isinstance(parsed, list):
		frappe.throw(_("`{0}` must be a list.").format(label))
	return parsed


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@frappe.whitelist()
def admin_is_admin():
	"""Cheap capability probe for the app.

	Returns a flag rather than throwing, so the UI can hide admin controls for a
	normal shopper instead of rendering a button that fails when pressed.
	"""
	if frappe.session.user == "Guest":
		return {"is_admin": False}
	try:
		return {"is_admin": _is_admin(), "user": frappe.session.user}
	except Exception:
		# A misconfigured admin table must not break the whole catalogue for every
	# customer, so this probe degrades to "not an admin" and logs. Real admin
	# actions still throw via _require_admin, where the cause is visible.
		frappe.log_error(title="admin_users lookup failed")
		return {"is_admin": False}


@frappe.whitelist()
def admin_reference_data():
	"""Item groups, UOMs and warehouses, for the create-product form.

	Split from `admin_item_payload` because creating a product is exactly the
	case where there is no item_code to look one up by, and the create form needs
	the same choices the edit form shows.
	"""
	_require_admin()
	return _reference_data()


@frappe.whitelist()
def admin_item_payload(item_code=None):
	"""Everything the editor needs for one product, in one round trip."""
	_require_admin()
	item_code = (item_code or "").strip()
	if not item_code or not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item not found."), frappe.DoesNotExistError)

	item = _get_one(
		"Item",
		item_code,
		["name", "item_name", "item_group", "stock_uom", "description", "image"],
	)

	website_item_name = frappe.db.get_value("Website Item", {"item_code": item_code}, "name")
	website_item = _get_one(
		"Website Item",
		website_item_name,
		[
			"name",
			"route",
			"web_item_name",
			"item_name",
			"short_description",
			"web_long_description",
			"ranking",
			"published",
			"website_warehouse",
			"website_image",
		],
	)

	recommended = _recommended_rows(website_item_name)

	return {
		"item_code": item_code,
		"item": item,
		"website_item": website_item,
		"has_website_item": bool(website_item_name),
		"uoms": _uom_rows(item_code),
		"recommended_items": recommended,
		"references": _reference_data(),
	}


def _recommended_rows(website_item_name):
	"""Recommended products, resolved to something the app can render."""
	if not website_item_name:
		return []

	rows = []
	for entry in (frappe.get_doc("Website Item", website_item_name).recommended_items or []):
		code = entry.item_code
		if not code:
			continue
		detail = frappe.db.get_value(
			"Website Item",
			{"item_code": code},
			["web_item_name", "item_name", "website_image", "route", "published"],
			as_dict=True,
		)
		rows.append(
			{
				"item_code": code,
				"web_item_name": (detail and (detail.web_item_name or detail.item_name)) or code,
				"image": (detail and detail.website_image) or "",
				"route": (detail and detail.route) or "",
				"published": cint(detail and detail.published),
			}
		)
	return rows


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@frappe.whitelist()
def admin_update_item(
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
	"""Apply an edit. Only the arguments actually sent are changed.

	Every parameter defaults to None so a partial save cannot blank a field the
	editor did not load — the failure mode of an "apply everything" endpoint is a
	told-together page quietly erasing the product description.
	"""
	_require_admin()
	item_code = (item_code or "").strip()
	if not item_code or not frappe.db.exists("Item", item_code):
		frappe.throw(_("Item not found."), frappe.DoesNotExistError)

	warnings = []

	# --- name, shown on both the master record and the storefront -------------
	if item_name is not None:
		new_name = strip_html((item_name or "").strip())[:140]
		if not new_name:
			frappe.throw(_("The item name cannot be empty."))
		if new_name != frappe.db.get_value("Item", item_code, "item_name"):
			# Renaming an Item is more than a label: ERPNext keys the Website Item
			# off item_code but shows these, and the route is generated from the
			# web name. All four are written together so no surface keeps the old
			# name.
			frappe.db.set_value("Item", item_code, "item_name", new_name)
			website_item_name = frappe.db.get_value("Website Item", {"item_code": item_code}, "name")
			if website_item_name:
				frappe.db.set_value(
					"Website Item", 
					website_item_name, 
					{
						"item_name": new_name, 
						"web_item_name": new_name
					}
				)
				warnings.extend(_resync_route(website_item_name))

	# --- group ----------------------------------------------------------------
	if item_group is not None:
		group = (item_group or "").strip()
		if not frappe.db.exists("Item Group", group):
			frappe.throw(_("Item Group `{0}` does not exist.").format(group))
		frappe.db.set_value("Item", item_code, "item_group", group)
		website_item_name = frappe.db.get_value("Website Item", {"item_code": item_code}, "name")
		if website_item_name:
			frappe.db.set_value("Website Item", website_item_name, "item_group", group)
			# The catalogue filters on `item_group IN (groups shown in website)`.
			# Moving a product into a hidden group makes it disappear from the
			# app and the site with no error anywhere, so it is reported.
			if not frappe.db.get_value("Item Group", group, "show_in_website"):
				warnings.append(
					_("`{0}` is hidden from the website, so this product will not be listed.").format(group)
				)

	# --- descriptions ---------------------------------------------------------
	website_item_name = frappe.db.get_value("Website Item", {"item_code": item_code}, "name")
	if short_description is not None:
		clean = strip_html(short_description or "").strip()
		frappe.db.set_value("Item", item_code, "description", clean[:500])
		if website_item_name:
			frappe.db.set_value("Website Item", website_item_name, "short_description", clean[:300])

	if web_long_description is not None:
		if not website_item_name:
			warnings.append(_("This product has no Website Item yet, so the long description was not saved."))
		else:
			frappe.db.set_value(
				"Website Item", website_item_name, "web_long_description", (web_long_description or "").strip()
			)

	if ranking is not None and website_item_name:
		frappe.db.set_value("Website Item", website_item_name, "ranking", cint(ranking))

	if published is not None and website_item_name:
		frappe.db.set_value("Website Item", website_item_name, "published", cint(published))

	# --- UOMs and prices ------------------------------------------------------
	if uoms_json is not None:
		_apply_uoms(item_code, _parse_json_list(uoms_json, "uoms_json"))

	# --- recommended ----------------------------------------------------------
	if recommended_json is not None:
		if not website_item_name:
			warnings.append(_("This product has no Website Item yet, so recommendations were not saved."))
		else:
			_apply_recommended(website_item_name, item_code, _parse_json_list(recommended_json, "recommended_json"))

	# --- image ----------------------------------------------------------------
	image_url = None
	if image_base64:
		image_url = _save_item_image(item_code, image_base64, image_name)

	precision_note = _float_precision_warning()
	if precision_note:
		warnings.append(precision_note)

	frappe.db.commit()

	return {
		"ok": True,
		"image": image_url,
		"warnings": warnings,
		"item": admin_item_payload(item_code),
	}


def _resync_route(website_item_name):
	"""Regenerate the product URL after a rename.

	The route is cleared so ERPNext rebuilds it from the new web_item_name using the
	site's own configured base path. Re-implementing that slug logic here would drift
	from the website the first time either side changed.

	Note the document is *saved*, not patched. `frappe.db.set_value(...,"route","")`
	writes the column directly and skips `Document.validate()`, which is where
	ERPNext calls `set_route()` — so the route would stay empty and every single
	rename would come back with the "could not be rebuilt" warning.
	"""
	frappe.db.set_value("Website Item", website_item_name, "route", "")

	def _current_route():
		return frappe.db.get_value("Website Item", website_item_name, "route")

	# The normal hook chain first, which is the only version-accurate way to get
	# the site's real route format.
	doc = frappe.get_doc("Website Item", website_item_name)
	try:
		doc.save(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Website Item route rebuild failed")

	# Older/patched ERPNext builds only call set_route() from the Website Item
	# generator, so fall back to invoking it directly.
	if not _current_route():
		try:
			doc.set_route()
			doc.save(ignore_permissions=True)
		except Exception:
			frappe.log_error(title="Website Item set_route() failed")

	route = _current_route()
	if not route:
		return [
			_(
				"The product URL could not be rebuilt automatically. Set a Route on the Website Item before publishing."
			)
		]
	return [_("The product URL changed to `{0}`. Older links to the previous URL will not resolve.").format(route)]


def _apply_recommended(website_item_name, item_code, rows):
	"""Replace the recommended-products list.

	Self-references are dropped rather than rejected: a recommendation carousel
	that points at its own product is a mistake, but blocking the whole save over
	one stray row is worse than ignoring it.
	"""
	doc = frappe.get_doc("Website Item", website_item_name)
	for existing in list(doc.recommended_items or []):
		doc.remove(existing)

	for row in rows:
		code = (row.get("item_code") or "").strip()
		if not code or code == item_code:
			continue
		if not frappe.db.exists("Item", code):
			frappe.throw(_("`{0}` is not a valid item code.").format(code))
		doc.append("recommended_items", {"item_code": code})

	doc.save()


def _unique_item_code(preferred, name):
	"""Return a free Item code derived from the product name.

	The create form sends the name in place of a code, so this is what stands
	between a product and a duplicate-code error. Three cases, in order:

	- `preferred` is the ASCII slug of the name, used as-is while it is free.
	- The slug comes back empty when the name holds no Latin letters or digits —
	  `slugify` keeps only `[a-z0-9\\s-]`, so an Arabic name reduces to nothing.
	  The name itself is then used: `item_code` is free text in ERPNext, so a
	  non-Latin code is valid, and one the admin can recognise beats an opaque
	  generated identifier.
	- A clash gets a numeric suffix rather than an error. Two products may
	  legitimately share a name, and refusing the second one outright is a worse
	  failure than a suffixed code.
	"""
	base = preferred or (name or "").strip()
	if not base:
		frappe.throw(_("An item name is required."))
	base = base[:140]

	def taken(candidate):
		return frappe.db.exists("Item", candidate) or frappe.db.exists(
			"Website Item", {"item_code": candidate}
		)

	if not taken(base):
		return base

	# Truncate to leave room for the suffix rather than letting the suffix push
	# the code past the column, which would be a database error rather than a
	# message the admin can act on.
	for suffix in range(2, 1000):
		tail = "-{0}".format(suffix)
		candidate = "{0}{1}".format(base[: 140 - len(tail)], tail)
		if not taken(candidate):
			return candidate

	frappe.throw(_("Could not derive a unique item code from `{0}`.").format(base))


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@frappe.whitelist()
def admin_create_item(
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
	"""Create a product across Item, Item Price and Website Item.

	Published by default, because the stated intent is to put the product in the
	shop. A draft is one flag away in the editor.
	"""
	_require_admin()

	name = strip_html((item_name or "").strip())
	# The create form no longer asks for a code: the name is the only identifier
	# an admin has to supply. The code is derived from it here.
	code = _unique_item_code(slugify((item_code or item_name or "").strip()), name)
	# An older client can still send a code with no name; fall back to it rather
	# than inserting an Item with a blank item_name.
	name = name or code
	group = (item_group or "").strip()
	if not frappe.db.exists("Item Group", group):
		frappe.throw(_("Item Group `{0}` does not exist.").format(group))

	base_uom = (stock_uom or "").strip()
	if not frappe.db.exists("UOM", base_uom):
		frappe.throw(_("Unit of Measure `{0}` does not exist.").format(base_uom))

	settings = _settings()
	if not settings.company:
		frappe.throw(_("Configure a company in Webshop Settings before creating products."))

	uom_rows = _parse_json_list(uoms_json, "uoms_json")
	for row in uom_rows:
		uom = (row.get("uom") or "").strip()
		if not frappe.db.exists("UOM", uom):
			frappe.throw(_("Unit of Measure `{0}` does not exist.").format(uom))
		# Every unit is new here, so there is no stored factor to fall back on:
		# unlike the editor, a missing factor is always an error.
		if uom != base_uom and (_decimal(row.get("conversion_factor")) or 0) <= 0:
			frappe.throw(
				_("Conversion rate for `{0}` must be greater than zero (received `{1}`).").format(
					uom, row.get("conversion_factor")
				)
			)

	short = strip_html(short_description or "").strip()

	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": name[:140],
			"item_group": group,
			"stock_uom": base_uom,
			"description": short[:500],
			"is_stock_item": 1,
			"uoms": [
				{
					"uom": (row.get("uom") or "").strip(),
					"conversion_factor": 1.0
					if (row.get("uom") or "").strip() == base_uom
					else _decimal(row.get("conversion_factor")),
				}
				for row in uom_rows
				if (row.get("uom") or "").strip()
			],
		}
	)
	item.insert(ignore_permissions=True)
	code = item.item_code
	website_item = frappe.get_doc(
		{
			"doctype": "Website Item",
			"item_code": code,
			"web_item_name": name[:140],
			"item_group": group,
			"website_warehouse": (website_warehouse or settings.website_warehouse or None),
			"short_description": short[:300],
			"web_long_description": (web_long_description or "").strip(),
			"ranking": cint(ranking or 0),
			"published": cint(published),
		}
	)
	website_item.insert(ignore_permissions=True)
	_ensure_route(website_item)

	# The base unit always needs a price, or the product cannot be added to a
	# cart at all — `ecommerce.api.cart` refuses a zero rate.
	base_rate = flt(price_list_rate, 0)
	if base_rate <= 0:
		frappe.throw(_("A selling price is required for the base unit of measure."))
	_apply_item_price(code, base_uom, base_rate)

	for row in uom_rows:
		uom = (row.get("uom") or "").strip()
		rate = flt(row.get("price_list_rate"), 0)
		if uom and uom != base_uom and rate > 0:
			_apply_item_price(code, uom, rate)

	image_url = None
	if image_base64:
		image_url = _save_item_image(code, image_base64, image_name)

	if recommended_json:
		_apply_recommended(website_item.name, code, _parse_json_list(recommended_json, "recommended_json"))

	frappe.db.commit()

	return {
		"ok": True,
		"item_code": code,
		"website_item": website_item.name,
		"route": frappe.db.get_value("Website Item", website_item.name, "route"),
		"image": image_url,
		"warnings": _creation_warnings(group, cint(published)),
	}


def _ensure_route(website_item):
	"""Make sure a new product has a working URL.

	ERPNext normally builds this itself. If it does not, a slug is used, and if
	that also fails the product is left for the shop owner rather than published
	under a blank URL.
	"""
	frappe.db.commit()
	route = frappe.db.get_value("Website Item", website_item.name, "route")
	if route:
		return

	try:
		website_item.set_route()
		website_item.save(ignore_permissions=True)
	except Exception:
		frappe.db.set_value(
			"Website Item", website_item.name, "route", "products/{0}".format(slugify(website_item.item_name or website_item.item_code))
		)

	frappe.db.commit()


def _creation_warnings(group, published):
	warnings = []
	if not frappe.db.get_value("Item Group", group, "show_in_website"):
		warnings.append(
			_("`{0}` is hidden from the website, so this product will not be listed until you enable it.").format(group)
		)
	if not published:
		warnings.append(_("The product was created unpublished and is not visible in the shop."))
	return warnings
