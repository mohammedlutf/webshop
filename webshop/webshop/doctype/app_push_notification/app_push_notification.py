import frappe
from frappe.model.document import Document
import requests

_EXPO_PUSH_ENDPOINT = "https://exp.host/--/api/v2/push/send"
_EXPO_BATCH_SIZE = 100


class AppPushNotification(Document):
	def validate(self):
		if not self.title or not self.message:
			frappe.throw("Push notifications require both a title and a message body.")

	@frappe.whitelist()
	def send_push_notification(self):
		"""Dispatch the campaign to every registered device in the audience.

		The Expo HTTP API answers with one *ticket* per message, not a single
		boolean. `status == ok` on the HTTP response only means "Expo accepted
		the batch"; the per-ticket `status` field is the real delivery verdict.
		That is why the old code (which counted the whole batch as delivered on
		a 200) could report "sent to 100 devices" for a batch where every token
		was invalid.
		"""
		if self.status == "Sent":
			frappe.throw("This campaign has already been sent.")

		# Audience -> device filter. "All Devices" sends to both guest tokens and
		# account-bound tokens (whose `is_guest` is 0).
		filters = {}
		if self.target_audience == "Guests Only":
			filters["is_guest"] = 1
		elif self.target_audience == "Logged In Users Only":
			filters["is_guest"] = 0

		rows = frappe.get_all(
			"App Device Token",
			filters=filters,
			fields=["name", "device_token"],
			order_by="last_active desc",
			limit_page_length=0,
		)
		if not rows:
			frappe.throw("No device tokens found matching the selected target audience.")

		headers = {
			"Accept": "application/json",
			"Accept-Encoding": "gzip, deflate",
			"Content-Type": "application/json",
		}

		# `data` is delivered to the app verbatim on every platform and is how
		# the inbox screen identifies the campaign. `campaign_id` is the
		# document name; `doctype` lets the app route generic notifications.
		payload = {
			"campaign_id": self.name,
			"doctype": "App Push Notification",
			"target_audience": self.target_audience,
		}

		successful = 0
		failed = 0
		dead_tokens = []

		for start in range(0, len(rows), _EXPO_BATCH_SIZE):
			batch = rows[start : start + _EXPO_BATCH_SIZE]
			messages = [
				{
					"to": row.device_token,
					"sound": "default",
					"title": self.title,
					"body": self.message,
					"data": payload,
					"priority": "high",
				}
				for row in batch
			]

			try:
				response = requests.post(
					_EXPO_PUSH_ENDPOINT,
					json=messages,
					headers=headers,
					timeout=30,
				)
			except Exception as e:
				_error("Push Campaign HTTP error: {}".format(e), self.name)
				# One network blip must not abort the whole campaign: mark the
				# batch unsent and move on to the next one.
				failed += len(batch)
				continue

			if response.status_code != 200:
				failed += len(batch)
				_error(
					"Push Campaign HTTP {} for campaign {}: {}".format(
						response.status_code, self.name, _safe_body(response)
					),
					self.name,
				)
				continue

			try:
				tickets = response.json().get("data", [])
			except ValueError:
				failed += len(batch)
				_error("Push Campaign invalid JSON response", self.name)
				continue

			for row, ticket in zip(batch, tickets):
				if not isinstance(ticket, dict):
					failed += 1
					continue
				if ticket.get("status") == "ok":
					successful += 1
					continue

				details = ticket.get("details") or {}
				reason = details.get("error") or ticket.get("message") or "Unknown error"

				# The fastest way to detect a stale token: Expo tells us the
				# device is no longer reachable. Delete the row so future
				# campaigns stop wasting a slot in the batch.
				if reason == "DeviceNotRegistered":
					dead_tokens.append(row.name)
				failed += 1
				_error(
					"Push ticket error for token {} ({}): {}".format(
						row.name, row.device_token, reason
					),
					self.name,
				)

		if dead_tokens:
			for name in dead_tokens:
				try:
					frappe.delete_doc("App Device Token", name, force=True, ignore_permissions=True)
				except Exception as e:
					_error("Could not remove dead token {}: {}".format(name, e), self.name)

		self.status = "Sent" if successful > 0 else "Failed"
		self.sent_count = successful
		self.flags.ignore_permissions = True
		self.save(ignore_permissions=True)
		frappe.db.commit()

		return {
			"status": "completed",
			"delivered_count": successful,
			"failed_count": failed,
			"removed_tokens": len(dead_tokens),
		}


def _safe_body(response):
	try:
		return (response.text or "")[:500]
	except Exception:
		return ""


def _error(message, campaign_name=""):
	frappe.log_error(
		"{} — campaign: {}".format(message, campaign_name or "unknown"),
		"App Push Notification",
	)