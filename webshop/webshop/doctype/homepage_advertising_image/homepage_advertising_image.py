"""Child table of `Webshop Settings`: the banners for the app's home hero.

One row is one slide. Only the image itself is required — a banner with no
target is still a perfectly good banner — so the target fields are all
optional, and the storefront picks the most specific one that is set
(product, then item group, then URL).
"""

from frappe.model.document import Document


class HomepageAdvertisingImage(Document):
	pass
