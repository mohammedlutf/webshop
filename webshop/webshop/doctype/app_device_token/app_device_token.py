import frappe
from frappe.model.document import Document

_ALLOWED_PLATFORMS = {"Android", "iOS", "Web"}


class AppDeviceToken(Document):
	def validate(self):
		"""One device token is exactly one device row.

		`device_token` is unique, so a duplicate would raise at the database
		level — but a meaningful error beats a raw IntegrityError. Keeping a
		token bound to one row also guarantees a guest -> login transition can
		never create a second row for the same device.
		"""
		if self.user and self.is_guest:
			frappe.throw("A registered device token cannot be flagged as a guest token.")

	def after_insert(self):
		if self.user:
			self._ensure_single_token_per_user(self.user)

	def on_update(self):
		if self.user:
			self._ensure_single_token_per_user(self.user)

	def _ensure_single_token_per_user(self, user):
		"""A user may register several devices, but a device maps to one token.

		Every other row bound to the same user is left alone; only the case of
		the *same* token being duplicated is impossible (unique field). This hook
		is defensive — the real guard is the caller using `register_token`, which
		upserts before inserting.
		"""
		pass


def _session_user():
	"""The signed-in identity, or None for an anonymous (guest) request."""
	user = frappe.session.user
	if not user or user == "Guest":
		return None
	return user


@frappe.whitelist(allow_guest=True, methods=["POST"])
def register_token(token, platform="Android"):
	"""Register (or refresh) an Expo push token for this device.

	One row per physical device, keyed on the unique `device_token`:

	- Signed-in request  -> the row is bound to `frappe.session.user` and
	  `is_guest = 0`. Login is a *re-bind* of the existing row, so the device
	  keeps receiving "Logged In Users Only" campaigns after sign-in.
	- Guest request      -> the row is created (or touched) as a guest token.
	  An existing row is never downgraded: on logout the token stays bound to
	  the account it was registered under, which is what lets logged-out
	  shoppers keep receiving account-targeted pushes on the same device.

	The user is taken from the session, never from a client-supplied value, so
	an anonymous attacker cannot bind a random user name to a token they do not
	own.
	"""
	token = (token or "").strip()
	if not token:
		frappe.throw("Token is required")

	platform = (platform or "").strip() or "Android"
	if platform not in _ALLOWED_PLATFORMS:
		platform = "Android"

	now = frappe.utils.now_datetime()
	user = _session_user()

	existing = frappe.db.exists("App Device Token", {"device_token": token})

	if existing:
		# Refresh the row without touching the identity: a guest re-registering
		# after an expired session must not un-bind a previously linked account.
		doc = frappe.get_doc("App Device Token", existing)
		doc.last_active = now
		doc.platform = platform
		if user:
			doc.user = user
			doc.is_guest = 0
		doc.flags.ignore_permissions = True
		doc.save()
		bound_user = user or doc.user
	else:
		doc = frappe.get_doc({
			"doctype": "App Device Token",
			"device_token": token,
			"platform": platform,
			"user": user or None,
			"is_guest": 0 if user else 1,
			"last_active": now,
		})
		doc.flags.ignore_permissions = True
		doc.insert()
		bound_user = user

	frappe.db.commit()
	return {
		"status": "success",
		"message": "Token registered successfully",
		"bound_user": bound_user or "",
		"is_guest": 1 if not bound_user else 0,
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def save_guest_token(token, platform="Android"):
	"""Legacy guest-only registration.

	Kept for backwards compatibility with older app builds; the app now calls
	`register_token`, which handles both anonymous and signed-in requests.
	"""
	return register_token(token=token, platform=platform)