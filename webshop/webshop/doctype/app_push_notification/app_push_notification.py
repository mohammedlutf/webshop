import os
import re

import frappe
from frappe.model.document import Document

try:
	import firebase_admin
	from firebase_admin import credentials, messaging
except ImportError:  # firebase-admin is a bench-level dependency, not an app one
	firebase_admin = None
	credentials = None
	messaging = None

# The Firebase service-account key lives next to the App Device Token doctype
# (i.e. the same folder the app is deployed into). It authorises this server to
# talk to FCM directly, so no FCM credentials ever need to be handed to Expo/EAS.
_SERVICE_ACCOUNT_FILENAME = frappe.conf.get("fcm_service_account_path")
# _SERVICE_ACCOUNT_FILENAME = "ecommerce-efd8b-firebase-adminsdk-fbsvc-94ee7d13d2.json"
_SERVICE_ACCOUNT_HINT = "firebase-adminsdk"


# A *named* Firebase app keeps us from clobbering anything else on the bench
# that already initialised the SDK under the default name.
_FIREBASE_APP_NAME = "zad-market-fcm"
_firebase_app = None

# FCM accepts at most 500 tokens per multicast request.
_BATCH_SIZE = 500

# Must match the channel the app creates at runtime (DEFAULT_CHANNEL_ID in
# src/context/NotificationContext.js) and the plugin's `defaultChannel`.
_ANDROID_CHANNEL_ID = "default_v2"

# Tokens that are *not* FCM registration tokens. Expo's push service and a bare
# APNs device token both look different from an FCM token, and neither can be
# addressed through the FCM HTTP v1 API.
_EXPO_PREFIXES = ("ExponentPushToken[", "ExpoPushToken[")
_APNS_TOKEN = re.compile(r"^[0-9a-fA-F]{64}$")


class AppPushNotification(Document):
	def validate(self):
		if not self.title or not self.message:
			frappe.throw("Push notifications require both a title and a message body.")

	@frappe.whitelist()
	def send_push_notification(self):
		"""Dispatch the campaign to every registered device through FCM (HTTP v1).

		Delivery goes straight from this ERPNext site to Google using the
		service-account key stored beside the App Device Token doctype. Nothing
		is routed through Expo's push service, so the app only has to hand us a
		native FCM registration token (see `register_token`).

		`messaging.send_each_for_multicast` returns one result *per token*, not a
		single boolean, which is what lets us tell a real delivery from a stale
		token and prune the latter.
		"""
		if not _fcm_available():
			frappe.throw(
				"firebase-admin is not installed on this bench. Run "
				"`pip install firebase-admin`, then restart the workers."
			)

		# Audience -> device filter. "All Devices" targets both guest tokens and
		# account-bound tokens (whose `is_guest` is 0).
		filters = {}
		if self.target_audience == "Guests Only":
			filters["is_guest"] = 1
		elif self.target_audience == "Logged In Users Only":
			filters["is_guest"] = 0

		rows = frappe.get_all(
			"App Device Token",
			filters=filters,
			fields=["name", "device_token", "platform"],
			order_by="last_active desc",
			limit_page_length=0,
		)
		if not rows:
			frappe.throw("No device tokens found matching the selected target audience.")

		# This sender can only address FCM registration tokens. Any legacy
		# Expo/APNs rows are skipped (and reported) instead of failing the whole
		# campaign on the first unusable token.
		sendable = []
		skipped = []
		for row in rows:
			token = (row.device_token or "").strip()
			if not token:
				continue
			if _token_kind(token) == "fcm":
				sendable.append(row)
			else:
				skipped.append(row)

		data = {
			"campaign_id": str(self.name),
			"doctype": "App Push Notification",
			"target_audience": str(self.target_audience or ""),
			"title": str(self.title or ""),
			"body": str(self.message or ""),
		}

		delivered = 0
		failed = 0
		removed = 0

		if sendable:
			app = _get_firebase_app()
			name_by_token = {r.device_token.strip(): r.name for r in sendable}
			results = _deliver(
				app,
				[r.device_token.strip() for r in sendable],
				self.title,
				self.message,
				data,
			)
			dead = []
			for res in results:
				if res["ok"]:
					delivered += 1
					continue
				failed += 1
				name = name_by_token.get(res["token"])
				if _is_dead_token(res["exception"]) and name:
					dead.append(name)
				_error(
					"FCM send failed for {} ({}): {}".format(
						name or res["token"], res["token"], res["error"]
					),
					self.name,
				)

			# Unregistered / wrong-project tokens can never succeed again: drop
			# them so future campaigns stop spending a slot on them.
			for name in dead:
				try:
					frappe.delete_doc(
						"App Device Token", name, force=True, ignore_permissions=True
					)
					removed += 1
				except Exception as exc:
					_error("Could not remove dead token {}: {}".format(name, exc), self.name)
		else:
			_error(
				"No FCM tokens for campaign {} ({} non-FCM token(s) skipped).".format(
					self.name, len(skipped)
				),
				self.name,
			)

		if skipped:
			_error(
				"Skipped {} non-FCM device token(s) on campaign {}: iOS/APNs and legacy "
				"Expo tokens cannot be sent through FCM.".format(len(skipped), self.name),
				self.name,
			)

		self.status = "Sent" if delivered > 0 else "Failed"
		self.sent_count = delivered
		self.flags.ignore_permissions = True
		self.save(ignore_permissions=True)
		frappe.db.commit()

		return {
			"status": "completed",
			"delivered_count": delivered,
			"failed_count": failed,
			"removed_tokens": removed,
			"skipped_count": len(skipped),
		}


# --- Firebase plumbing --------------------------------------------------------


def _fcm_available():
	return firebase_admin is not None and credentials is not None and messaging is not None


def _service_account_path():
	"""Resolve the Firebase service-account JSON on disk.

	Precedence: an explicit `fcm_service_account_path` in site_config.json, then
	the exact key shipped next to the App Device Token doctype, then any
	`*firebase-adminsdk*.json` dropped in that folder.
	"""
	configured = frappe.conf.get("fcm_service_account_path") or frappe.conf.get(
		"firebase_service_account"
	)
	if configured and os.path.exists(configured):
		return configured

	# .../<app>/<module>/doctype/app_push_notification/app_push_notification.py
	doctype_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
	candidates = [
		os.path.join(doctype_dir, "app_device_token", _SERVICE_ACCOUNT_FILENAME),
		os.path.join(doctype_dir, _SERVICE_ACCOUNT_FILENAME),
	]
	for candidate in candidates:
		if os.path.exists(candidate):
			return candidate

	for folder in (os.path.join(doctype_dir, "app_device_token"), doctype_dir):
		if os.path.isdir(folder):
			for entry in os.listdir(folder):
				if entry.endswith(".json") and _SERVICE_ACCOUNT_HINT in entry:
					return os.path.join(folder, entry)

	frappe.throw(
		"Firebase service-account key not found. Expected "
		"'{0}' beside the App Device Token doctype, or set "
		"`fcm_service_account_path` in site_config.json.".format(_SERVICE_ACCOUNT_FILENAME)
	)


def _get_firebase_app():
	"""Return a cached, named firebase_admin app (initialised on first use)."""
	global _firebase_app
	if _firebase_app is not None:
		return _firebase_app
	try:
		_firebase_app = firebase_admin.get_app(_FIREBASE_APP_NAME)
	except ValueError:
		# Not initialised yet under this name.
		cred = credentials.Certificate(_service_account_path())
		_firebase_app = firebase_admin.initialize_app(cred, name=_FIREBASE_APP_NAME)
	return _firebase_app


def _deliver(app, tokens, title, body, data):
	"""Send one campaign to `tokens`; return per-token result dicts.

	Each result is {"token", "ok", "error", "exception"} so the caller can decide
	which failures mean "this token is dead".
	"""
	results = []
	android = messaging.AndroidConfig(
		priority="high",
		notification=messaging.AndroidNotification(
			channel_id=_ANDROID_CHANNEL_ID, sound="default"
		),
	)
	apns = messaging.APNSConfig(
		headers={"apns-priority": "10"},
		payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")),
	)

	use_multicast = hasattr(messaging, "send_each_for_multicast")

	for chunk in _chunks(tokens, _BATCH_SIZE):
		if use_multicast:
			message = messaging.MulticastMessage(
				tokens=chunk,
				notification=messaging.Notification(title=title, body=body),
				data=data,
				android=android,
				apns=apns,
			)
			try:
				batch = messaging.send_each_for_multicast(message, app=app)
			except Exception as exc:
				# Whole request failed (auth/network/quota): attribute it to the
				# batch rather than pretending individual tokens are dead.
				for token in chunk:
					results.append(_result(token, False, exc))
				continue
			for token, single in zip(chunk, batch.responses):
				if single.success:
					results.append(_result(token, True, None))
				else:
					results.append(_result(token, False, single.exception))
		else:
			# Older firebase-admin without multicast: one request per token.
			for token in chunk:
				message = messaging.Message(
					token=token,
					notification=messaging.Notification(title=title, body=body),
					data=data,
					android=android,
					apns=apns,
				)
				try:
					messaging.send(message, app=app)
					results.append(_result(token, True, None))
				except Exception as exc:
					results.append(_result(token, False, exc))
	return results


def _result(token, ok, exception):
	return {
		"token": token,
		"ok": ok,
		"error": None if ok else _describe(exception),
		"exception": exception,
	}


def _chunks(seq, size):
	for start in range(0, len(seq), size):
		yield seq[start : start + size]


def _token_kind(token):
	"""Classify a stored token: 'fcm' | 'expo' | 'apns'."""
	if token.startswith(_EXPO_PREFIXES):
		return "expo"
	if _APNS_TOKEN.match(token):
		return "apns"
	return "fcm"


def _is_dead_token(exception):
	"""True when FCM says the token can never succeed again."""
	if exception is None or messaging is None:
		return False
	return isinstance(
		exception, (messaging.UnregisteredError, messaging.SenderIdMismatchError)
	)


def _describe(exception):
	if exception is None:
		return "Unknown error"
	name = type(exception).__name__
	detail = str(exception) or ""
	return "{}: {}".format(name, detail) if detail else name


def _error(message, campaign_name=""):
	frappe.log_error(
		"{} — campaign: {}".format(message, campaign_name or "unknown"),
		"App Push Notification",
	)
